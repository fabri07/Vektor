"""F-O.4 — `_capture_unclassified` devuelve `CaptureResult` y filtra filas
100% vacías y de agregado ADENTRO de la función (no en cada call site).

`_capture_unclassified` es `def` síncrono — solo hace `session.add(...)`, sin
`await` — así que se llama directo contra `db_session` sin mockear nada.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.ingestion_import_service import (
    CaptureResult,
    _apply_capture_result,
    _capture_unclassified,
)
from app.persistence.models.tenant import Tenant
from app.persistence.models.unclassified_record import UnclassifiedRecord


async def test_fila_con_contenido_real_se_captura(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    result = _capture_unclassified(
        db_session,
        sample_tenant.tenant_id,
        rows=[{"detalle": "Agua mineral", "monto": "500"}],
        headers=["detalle", "monto"],
        source="ingestion",
        uploaded_file_id=None,
        context_label="Fila real",
    )
    assert result == CaptureResult(captured=1, blank_skipped=0, aggregate_skipped=0)
    await db_session.commit()
    rows = (await db_session.execute(select(UnclassifiedRecord))).scalars().all()
    assert len(rows) == 1


async def test_fila_100_por_ciento_vacia_no_se_captura(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    result = _capture_unclassified(
        db_session,
        sample_tenant.tenant_id,
        rows=[{"detalle": None, "monto": ""}, {"detalle": "nan", "monto": "  "}],
        headers=["detalle", "monto"],
        source="ingestion",
        uploaded_file_id=None,
        context_label="Fila de relleno",
    )
    assert result == CaptureResult(captured=0, blank_skipped=2, aggregate_skipped=0)
    await db_session.commit()
    rows = (await db_session.execute(select(UnclassifiedRecord))).scalars().all()
    assert len(rows) == 0


async def test_fila_de_agregado_con_anchor_column_no_se_captura(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    result = _capture_unclassified(
        db_session,
        sample_tenant.tenant_id,
        rows=[{"fecha": "Subtotal", "monto": "18500"}],
        headers=["fecha", "monto"],
        source="ingestion",
        uploaded_file_id=None,
        context_label="Hoja sin clasificar",
        anchor_column="fecha",
    )
    assert result == CaptureResult(captured=0, blank_skipped=0, aggregate_skipped=1)
    await db_session.commit()
    rows = (await db_session.execute(select(UnclassifiedRecord))).scalars().all()
    assert len(rows) == 0


async def test_fecha_real_con_la_palabra_total_dentro_no_se_confunde(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Regresión explícita del criterio de palabra completa: una fila real
    cuya columna ancla contiene "Total" como subcadena de un texto libre más
    largo SÍ se captura (nunca se descarta como agregado)."""
    result = _capture_unclassified(
        db_session,
        sample_tenant.tenant_id,
        rows=[{"fecha": "Total facturado 15/03", "monto": "500"}],
        headers=["fecha", "monto"],
        source="ingestion",
        uploaded_file_id=None,
        context_label="Fila real con texto libre",
        anchor_column="fecha",
    )
    assert result == CaptureResult(captured=1, blank_skipped=0, aggregate_skipped=0)


async def test_lote_mixto_cuenta_cada_categoria_por_separado(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    result = _capture_unclassified(
        db_session,
        sample_tenant.tenant_id,
        rows=[
            {"fecha": "2024-03-15", "monto": "500"},  # real
            {"fecha": None, "monto": ""},  # vacía
            {"fecha": "Subtotal", "monto": "18500"},  # agregado
        ],
        headers=["fecha", "monto"],
        source="ingestion",
        uploaded_file_id=None,
        context_label="Hoja mixta",
        anchor_column="fecha",
    )
    assert result == CaptureResult(captured=1, blank_skipped=1, aggregate_skipped=1)


async def test_apply_capture_result_suma_contadores_y_devuelve_captured(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    counts: dict[str, object] = {}
    result = _capture_unclassified(
        db_session,
        sample_tenant.tenant_id,
        rows=[{"fecha": "2024-03-15", "monto": "500"}, {"fecha": "Total"}],
        headers=["fecha", "monto"],
        source="ingestion",
        uploaded_file_id=None,
        context_label="Hoja",
        anchor_column="fecha",
    )
    captured = _apply_capture_result(counts, result)
    assert captured == 1
    assert counts["filas_agregado"] == 1
    assert "filas_en_blanco" not in counts  # no se escribe la clave si no hubo ninguna
