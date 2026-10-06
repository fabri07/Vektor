"""Compuerta de relectura (B1, 2026-10-06): qué se reemplaza y qué se pierde.

Antes el preview decía «A anular: N», con N = todo lo no editado, incluido lo que
la relectura vuelve a crear. El dueño no podía distinguir una actualización de una
pérdida. Ahora el preview separa ``to_replace`` de ``to_lose``, y el apply exige
``accept_data_loss`` cuando algo se pierde.

El caso que lo motivó es real: en ASTERIA, el Libro Diario importó 7 devoluciones
de proveedor como VENTAS (antes de D0). Releerlo con D0 las saca de ventas y las
manda a «Otros». Eso es una pérdida de ventas que el dueño tiene que ver antes de
aplicar — y como D0 saca filas del medio del libro, corre el índice de todas las
ventas que venían después: medido por ``source_row_ref``, «se pierden» habría
dado las ÚLTIMAS ventas del libro, no las devoluciones.
"""

from __future__ import annotations

import io
import unittest.mock
import uuid
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import file_parsing, reread_service
from app.application.services.file_parsing import parse_uploaded_content
from app.application.services.ingestion_import_service import (
    default_confirmed_fields,
    insert_confirmed_data,
)
from app.integrations.s3 import S3Client
from app.persistence.models.file import PROCESSING_STATUS_DONE, UploadedFile
from app.persistence.models.repair import DataRepairRun
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.tests.conftest import add_business_profile

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.fixture(autouse=True)
def _sin_broker(mock_score_trigger: Any) -> None:
    """Sin broker en tests: ver el mismo fixture en ``test_reread_file.py``."""


@pytest_asyncio.fixture
async def tenant(db_session: AsyncSession) -> Tenant:
    t = Tenant(
        tenant_id=uuid.uuid4(),
        legal_name="Deco Test",
        display_name="Deco Test",
        currency="ARS",
        pricing_reference_mode="MEP",
        status="ACTIVE",
    )
    db_session.add(t)
    await db_session.flush()
    await add_business_profile(db_session, t.tenant_id)
    await db_session.commit()
    return t


def _patch_s3(monkeypatch: pytest.MonkeyPatch, content: bytes) -> None:
    async def _fake_download(self: S3Client, key: str) -> bytes:  # noqa: ARG001
        return content

    async def _fake_head(self: S3Client, key: str) -> dict[str, Any]:  # noqa: ARG001
        return {"etag": '"fake"', "size": len(content), "last_modified": "2026-01-01T00:00:00Z"}

    monkeypatch.setattr(S3Client, "download", _fake_download)
    monkeypatch.setattr(S3Client, "head", _fake_head)


async def _subir_e_importar(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    content: bytes,
    mime: str,
    filename: str,
    summary: dict[str, Any] | None = None,
    **import_kwargs: Any,
) -> UploadedFile:
    """Sube el archivo y lo importa. ``summary`` permite importar con OTRA lectura
    que la del parser actual (la de antes de un arreglo)."""
    summary = summary or parse_uploaded_content(content, mime, filename)
    confirmed = import_kwargs.pop("confirmed", None) or default_confirmed_fields(summary)
    f = UploadedFile(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        uploaded_by=None,
        original_filename=filename,
        s3_key=f"tenants/{tenant_id}/{filename}",
        content_type=mime,
        size_bytes=len(content),
        purpose="ventas",
        processing_status=PROCESSING_STATUS_DONE,
        parsed_summary_json={
            "inferred_type": summary.get("inferred_type"),
            "confirmed_fields": confirmed,
        },
    )
    session.add(f)
    await session.commit()
    await insert_confirmed_data(
        session,
        tenant_id,
        summary,
        confirmed,
        source="ingestion",
        uploaded_file_id=f.id,
        **import_kwargs,
    )
    await session.commit()
    return f


def _csv(filas: list[tuple[str, str, str]]) -> bytes:
    cuerpo = "".join(f"{d},{p},{m},Proveedor Sur\n" for d, p, m in filas)
    return ("fecha,producto,monto,proveedor\n" + cuerpo).encode()


_CUATRO = [
    ("2026-01-05", "Coca Cola", "1500"),
    ("2026-01-06", "Pan Lactal", "800"),
    ("2026-01-07", "Yerba Mate", "2200"),
    ("2026-01-08", "Fideos", "950"),
]


def _montos_perdidos(preview: reread_service.RereadPreview) -> list[Decimal]:
    return sorted(
        Decimal(c["before"]["amount"])
        for c in preview.sample_changes
        if c["action"] == "lose"
    )


# ── El caso ASTERIA: D0 aparta filas del medio del Libro Diario ────────────────

_LD_CON_APORTES: list[list[Any]] = [
    ["Fecha", "Detalle", "Dinero", None, "Mercadería", None],
    [None, None, "Ingreso", "Egreso", "Ingreso", "Egreso"],
    ["2026-05-01", "Venta del día", 120000, None, None, 80000],
    [None, "Aporte socio Juan", 500000, None, None, None],
    [None, "Préstamo de Ana", 200000, None, None, None],
    [None, "Devolución proveedor textiles", 7000, None, None, None],
    ["2026-05-02", "Mostrador", 30000, None, None, None],
    [None, "Cobro cliente Pérez", 15000, None, None, None],
]


def _xlsx(filas: list[list[Any]]) -> bytes:
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Libro Diario"
    for row in filas:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


async def test_releer_el_libro_diario_con_d0_muestra_las_devoluciones_en_se_pierden(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = _xlsx(_LD_CON_APORTES)
    # La importación original, con el parser de ANTES de D0: el detalle no
    # importaba, toda entrada de plata era venta.
    with monkeypatch.context() as m:
        m.setattr(file_parsing, "es_tipo_reconocido", lambda _d: False)
        viejo = parse_uploaded_content(content, _XLSX_MIME, "ld.xlsx")
    file = await _subir_e_importar(
        db_session, tenant.tenant_id, content, _XLSX_MIME, "ld.xlsx", summary=viejo
    )
    ventas = (
        await db_session.execute(select(SaleEntry).where(SaleEntry.source_upload_id == file.id))
    ).scalars().all()
    assert len(ventas) == 6, "precondición: antes de D0 el aporte entraba como venta"

    _patch_s3(monkeypatch, content)
    preview = await reread_service.preview_reread(db_session, file.id, tenant.tenant_id)

    assert preview.to_lose == 3
    assert preview.to_lose_to_others == 3
    assert preview.to_replace == 3
    assert preview.to_void == 6  # compatibilidad: sigue siendo reemplazo + pérdida
    assert preview.requires_data_loss_acceptance
    # Las que se pierden son el aporte, el préstamo y la devolución — NO las
    # últimas ventas del libro, que corrieron su índice.
    assert _montos_perdidos(preview) == [
        Decimal("7000.00"),
        Decimal("200000.00"),
        Decimal("500000.00"),
    ]
    destinos = [c["after"]["destino"] for c in preview.sample_changes if c["action"] == "lose"]
    assert all(d.startswith("Otros — ") for d in destinos)
    assert preview.edited_at_risk == 0


# ── Corrimiento de índices sin cambio de contenido ─────────────────────────────


async def test_una_fila_borrada_del_medio_se_pierde_ella_y_no_la_ultima(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Por referencia, la que «se pierde» sería la última posición (950); la que
    de verdad no vuelve es la del medio (800)."""
    file = await _subir_e_importar(
        db_session, tenant.tenant_id, _csv(_CUATRO), "text/csv", "gastos.csv"
    )
    sin_la_segunda = [_CUATRO[0], _CUATRO[2], _CUATRO[3]]
    _patch_s3(monkeypatch, _csv(sin_la_segunda))
    preview = await reread_service.preview_reread(db_session, file.id, tenant.tenant_id)

    assert (preview.to_replace, preview.to_lose) == (3, 1)
    assert preview.to_lose_to_others == 0
    assert _montos_perdidos(preview) == [Decimal("800.00")]
    perdido = next(c for c in preview.sample_changes if c["action"] == "lose")
    assert perdido["after"] is None  # no vuelve en ningún lado


async def test_una_fila_agregada_o_corregida_no_es_una_perdida(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    file = await _subir_e_importar(
        db_session, tenant.tenant_id, _csv(_CUATRO), "text/csv", "gastos.csv"
    )
    # Una fila nueva AL PRINCIPIO (corre todo) y una fecha corregida.
    nuevo = [("2026-01-04", "Azúcar", "640"), *_CUATRO]
    nuevo[2] = ("2026-01-16", "Pan Lactal", "800")
    _patch_s3(monkeypatch, _csv(nuevo))
    preview = await reread_service.preview_reread(db_session, file.id, tenant.tenant_id)

    assert (preview.to_replace, preview.to_lose) == (4, 0)
    assert not preview.requires_data_loss_acceptance


async def test_otra_operacion_el_mismo_dia_no_tapa_una_perdida(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Con importes confiables, emparejar por día escondería la pérdida: el pan
    de 800 no «se reemplaza» por un azúcar de 640 porque compartan la fecha."""
    file = await _subir_e_importar(
        db_session, tenant.tenant_id, _csv(_CUATRO), "text/csv", "gastos.csv"
    )
    otro = [_CUATRO[0], ("2026-01-06", "Azúcar", "640"), _CUATRO[2], _CUATRO[3]]
    _patch_s3(monkeypatch, _csv(otro))
    preview = await reread_service.preview_reread(db_session, file.id, tenant.tenant_id)

    assert (preview.to_replace, preview.to_lose) == (3, 1)
    assert _montos_perdidos(preview) == [Decimal("800.00")]


# ── El importe del import no siempre es el que lee el preview ──────────────────

_CTX_PxC = "sheet:Ventas"


def _summary_precio_por_cantidad(filas: list[tuple[str, str, str, str]]) -> dict[str, Any]:
    """Una hoja de ventas SIN total: el import lo calcula como precio × cantidad
    (F-H4) con el mapeo confirmado; el preview, que no ve el mapeo, lee el precio
    unitario como si fuera el importe."""
    headers = ["fecha", "producto", "precio_unit", "cant"]
    rows = [
        {"fecha": d, "producto": p, "precio_unit": pu, "cant": c, "__context__": _CTX_PxC}
        for d, p, pu, c in filas
    ]
    return {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "mapping_contexts": [
            {
                "context_id": _CTX_PxC,
                "entity_type": "sale",
                "source_kind": "sheet",
                "headers": headers,
                "fields": None,
                "preview_rows": [],
                "row_count": len(rows),
            }
        ],
        "ventas_detectadas": rows,
        "gastos_detectados": [],
        "stock_detectado": [],
    }


_PxC = [
    ("2026-03-10", "Vela", "150.50", "3"),
    ("2026-03-11", "Difusor", "900", "2"),
    ("2026-03-12", "Mantel", "4200", "1"),
    ("2026-03-13", "Florero", "1300", "4"),
]
_MAPEO_PxC = {
    "fecha": "transaction_date",
    "producto": "product_name",
    "precio_unit": "unit_price",
    "cant": "quantity",
}


async def test_precio_por_cantidad_releido_igual_no_pierde_nada(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    from copy import deepcopy

    summary = _summary_precio_por_cantidad(_PxC)
    file = await _subir_e_importar(
        db_session,
        tenant.tenant_id,
        b"pxc",
        _XLSX_MIME,
        "pxc.xlsx",
        summary=deepcopy(summary),
        confirmed={"ventas": True},
        context_mappings={_CTX_PxC: _MAPEO_PxC},
        context_confirmed={_CTX_PxC: True},
    )
    montos = sorted(
        s.amount
        for s in (
            await db_session.execute(select(SaleEntry).where(SaleEntry.source_upload_id == file.id))
        ).scalars()
    )
    assert Decimal("451.50") in montos, "precondición: el import calculó precio × cantidad"

    _patch_s3(monkeypatch, b"pxc")
    igual = await reread_service.preview_reread(
        db_session, file.id, tenant.tenant_id, fresh_override=deepcopy(summary)
    )
    assert (igual.to_replace, igual.to_lose) == (4, 0)

    # Sin el difusor (otro día): se pierde ESE, y nada más.
    sin_difusor = _summary_precio_por_cantidad([_PxC[0], _PxC[2], _PxC[3]])
    menos = await reread_service.preview_reread(
        db_session, file.id, tenant.tenant_id, fresh_override=sin_difusor
    )
    assert (menos.to_replace, menos.to_lose) == (3, 1)
    assert _montos_perdidos(menos) == [Decimal("1800.00")]


# ── Ediciones a mano cuya fila se corrió ───────────────────────────────────────


async def _editar(session: AsyncSession, file: UploadedFile, monto: str) -> None:
    rec = (
        await session.execute(
            select(ExpenseEntry).where(
                ExpenseEntry.source_upload_id == file.id,
                ExpenseEntry.amount == Decimal(monto),
            )
        )
    ).scalar_one()
    rec.has_user_edits = True
    rec.description = "corregido a mano"
    await session.commit()


async def test_una_edicion_despues_del_corrimiento_queda_en_riesgo(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    file = await _subir_e_importar(
        db_session, tenant.tenant_id, _csv(_CUATRO), "text/csv", "gastos.csv"
    )
    await _editar(db_session, file, "950")  # la última
    _patch_s3(monkeypatch, _csv(_CUATRO[1:]))  # se va la primera: todo se corre
    preview = await reread_service.preview_reread(db_session, file.id, tenant.tenant_id)

    assert preview.edited_at_risk == 1
    assert preview.requires_data_loss_acceptance
    riesgo = [c for c in preview.sample_changes if c["action"] == "edited_at_risk"]
    assert [Decimal(c["before"]["amount"]) for c in riesgo] == [Decimal("950.00")]


async def test_una_edicion_antes_del_corrimiento_no_esta_en_riesgo(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    file = await _subir_e_importar(
        db_session, tenant.tenant_id, _csv(_CUATRO), "text/csv", "gastos.csv"
    )
    await _editar(db_session, file, "1500")  # la primera
    _patch_s3(monkeypatch, _csv(_CUATRO[:3]))  # se va la última
    preview = await reread_service.preview_reread(db_session, file.id, tenant.tenant_id)

    assert preview.edited_at_risk == 0
    assert (preview.to_replace, preview.to_lose) == (2, 1)


# ════════════════════════════════════════════════════════════════════════════
# La AUTORIDAD: la medición exacta en el apply.
#
# Todo lo de arriba es el ESTIMADO del preview, que reconstruye qué crearía el
# import y por eso puede errar. La revisión adversarial lo midió: falsos
# negativos (una venta perdida sin aviso) y falsos positivos masivos. Lo que
# frena es esto: lo que el apply anuló contra lo que de verdad volvió a crear,
# dentro de su transacción. Cada test de acá corre el apply REAL.
# ════════════════════════════════════════════════════════════════════════════


async def _vivos(session: AsyncSession, file_id: uuid.UUID) -> tuple[int, int]:
    ventas = (
        await session.execute(
            select(SaleEntry).where(
                SaleEntry.source_upload_id == file_id, SaleEntry.voided_at.is_(None)
            )
        )
    ).scalars().all()
    gastos = (
        await session.execute(
            select(ExpenseEntry).where(
                ExpenseEntry.source_upload_id == file_id, ExpenseEntry.voided_at.is_(None)
            )
        )
    ).scalars().all()
    return len(ventas), len(gastos)


async def _aplicar_frena(
    session: AsyncSession, tenant_id: uuid.UUID, file_id: uuid.UUID, **kwargs: Any
) -> reread_service.PerdidaDeRelectura:
    """Aplica sin aceptar, espera que la compuerta frene y hace el rollback que
    hace el worker. Devuelve la medición. Recibe ids: el rollback expira los
    objetos, y leerlos en una sesión async rompe."""
    with pytest.raises(reread_service.RereadWouldLoseDataError) as frenado:
        await reread_service.apply_reread(session, file_id, tenant_id, **kwargs)
    await session.rollback()
    return frenado.value.perdida


async def test_apply_del_libro_diario_con_d0_frena_con_las_devoluciones_y_no_guarda_nada(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = _xlsx(_LD_CON_APORTES)
    with monkeypatch.context() as m:
        m.setattr(file_parsing, "es_tipo_reconocido", lambda _d: False)
        viejo = parse_uploaded_content(content, _XLSX_MIME, "ld.xlsx")
    file = await _subir_e_importar(
        db_session, tenant.tenant_id, content, _XLSX_MIME, "ld.xlsx", summary=viejo
    )
    tid, fid = tenant.tenant_id, file.id
    _patch_s3(monkeypatch, content)

    perdida = await _aplicar_frena(db_session, tid, fid)
    assert (perdida.to_lose, perdida.to_lose_to_others, perdida.edited_at_risk) == (3, 3, 0)
    montos = sorted(
        Decimal(c["before"]["amount"]) for c in perdida.samples if c["action"] == "lose"
    )
    assert montos == [Decimal("7000.00"), Decimal("200000.00"), Decimal("500000.00")]
    assert await _vivos(db_session, fid) == (6, 0), "frenado = nada aplicado"

    # Aceptado: se aplica, y lo perdido queda registrado en el run.
    resultado = await reread_service.apply_reread(db_session, fid, tid, accept_data_loss=True)
    await db_session.commit()
    assert await _vivos(db_session, fid) == (3, 0)
    run = await db_session.get(DataRepairRun, resultado.run_id)
    assert run is not None and (run.details_json or {})["data_loss"]["to_lose"] == 3


@pytest.mark.parametrize("fecha", ["xx/yy", "2099-01-06"])
async def test_una_fila_que_ahora_va_a_otros_por_su_fecha_frena(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch, fecha: str
) -> None:
    """Falso negativo del estimado (revisión): el preview la daba por
    reemplazada; el import la manda a «Otros» (ilegible) o la descarta (futura)."""
    tres = _CUATRO[:3]
    file = await _subir_e_importar(db_session, tenant.tenant_id, _csv(tres), "text/csv", "g.csv")
    tid, fid = tenant.tenant_id, file.id
    _patch_s3(monkeypatch, _csv([tres[0], (fecha, tres[1][1], tres[1][2]), tres[2]]))

    perdida = await _aplicar_frena(db_session, tid, fid)
    assert perdida.to_lose == 1
    assert [c["before"]["amount"] for c in perdida.samples if c["action"] == "lose"] == [
        "800.00"
    ]
    assert await _vivos(db_session, fid) == (0, 3)


async def test_una_edicion_que_vuelve_a_entrar_repetida_frena(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Falso negativo del estimado (revisión): [A, B, C editado] → [A, B, N, C].
    La huella de la edición ahora apunta a N, que se saltea; C entra de nuevo."""
    tres = _CUATRO[:3]
    file = await _subir_e_importar(db_session, tenant.tenant_id, _csv(tres), "text/csv", "g.csv")
    await _editar(db_session, file, "2200")
    tid, fid = tenant.tenant_id, file.id
    _patch_s3(monkeypatch, _csv([tres[0], tres[1], ("2026-01-06", "Azúcar", "333"), tres[2]]))

    perdida = await _aplicar_frena(db_session, tid, fid)
    assert perdida.edited_at_risk == 1
    riesgo = next(c for c in perdida.samples if c["action"] == "edited_at_risk")
    assert riesgo["before"]["description"] == "corregido a mano"
    assert riesgo["after"]["amount"] == "2200.00"


async def test_una_perdida_no_la_tapa_un_alta_del_mismo_importe_en_otro_dia(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revisión 2 (F2): [A, B 800 d06, C] → [A, C, F 800 d20]. Emparejar por
    importe sin texto daba to_lose=0 y B desaparecía. Es la forma de ASTERIA con
    D0 + Fase 3: se van filas y entran otras en la misma relectura."""
    tres = _CUATRO[:3]
    file = await _subir_e_importar(db_session, tenant.tenant_id, _csv(tres), "text/csv", "g.csv")
    tid, fid = tenant.tenant_id, file.id
    _patch_s3(monkeypatch, _csv([tres[0], tres[2], ("2026-01-20", "Facturas", "800")]))

    perdida = await _aplicar_frena(db_session, tid, fid)
    assert perdida.to_lose == 1
    assert [c["before"]["description"] for c in perdida.samples if c["action"] == "lose"] == [
        "Pan Lactal"
    ]


async def test_una_edicion_de_fecha_que_vuelve_a_entrar_frena(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revisión 2 (F1): el usuario corrigió la FECHA de C, y entra una fila al
    principio. La huella de C apunta ahora a B (que se saltea) y C vuelve a
    entrar con su fecha original: exigir el mismo día no la veía."""
    tres = _CUATRO[:3]
    file = await _subir_e_importar(db_session, tenant.tenant_id, _csv(tres), "text/csv", "g.csv")
    tid, fid = tenant.tenant_id, file.id
    rec = (
        await db_session.execute(
            select(ExpenseEntry).where(
                ExpenseEntry.source_upload_id == fid, ExpenseEntry.amount == Decimal("2200")
            )
        )
    ).scalar_one()
    from datetime import datetime

    rec.has_user_edits = True
    rec.transaction_date = datetime(2026, 1, 9)
    await db_session.commit()
    _patch_s3(monkeypatch, _csv([("2026-01-02", "Azúcar", "800"), *tres]))

    perdida = await _aplicar_frena(db_session, tid, fid)
    assert perdida.edited_at_risk == 1
    assert perdida.to_lose == 1  # Pan: su fila quedó bajo la huella de la edición


@pytest.mark.parametrize(
    "encabezado",
    ["fecha,detalle,venta,gasto", "fecha,concepto,ingreso,egreso"],
)
async def test_una_planilla_con_venta_y_gasto_por_fila_releida_igual_no_frena(
    db_session: AsyncSession,
    tenant: Tenant,
    monkeypatch: pytest.MonkeyPatch,
    encabezado: str,
) -> None:
    """Falso positivo del estimado (revisión): el preview decía «se pierden 3 o
    6» sobre el MISMO archivo."""
    content = (
        encabezado + "\n2026-01-05,A,1500,900\n2026-01-06,B,800,500\n2026-01-07,C,2200,1000\n"
    ).encode()
    file = await _subir_e_importar(
        db_session,
        tenant.tenant_id,
        content,
        "text/csv",
        "x.csv",
        confirmed={"ventas": True, "gastos": True},
    )
    tid, fid = tenant.tenant_id, file.id
    antes = await _vivos(db_session, fid)
    assert antes == (3, 3), "precondición: venta y gasto por fila"
    _patch_s3(monkeypatch, content)

    await reread_service.apply_reread(db_session, fid, tid)
    await db_session.commit()
    assert await _vivos(db_session, fid) == antes


@pytest.mark.parametrize(
    "corregida",
    [("2026-01-16", "Pan Lactal", "800"), ("2026-01-06", "Pan Lactal", "850")],
    ids=["fecha", "importe"],
)
async def test_una_fila_corregida_se_reemplaza_y_no_frena(
    db_session: AsyncSession,
    tenant: Tenant,
    monkeypatch: pytest.MonkeyPatch,
    corregida: tuple[str, str, str],
) -> None:
    file = await _subir_e_importar(
        db_session, tenant.tenant_id, _csv(_CUATRO), "text/csv", "gastos.csv"
    )
    tid, fid = tenant.tenant_id, file.id
    _patch_s3(monkeypatch, _csv([_CUATRO[0], corregida, *_CUATRO[2:]]))
    await reread_service.apply_reread(db_session, fid, tid)
    await db_session.commit()
    assert await _vivos(db_session, fid) == (0, 4)


async def test_otra_operacion_el_mismo_dia_no_tapa_la_perdida_en_el_apply(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    file = await _subir_e_importar(
        db_session, tenant.tenant_id, _csv(_CUATRO), "text/csv", "gastos.csv"
    )
    tid, fid = tenant.tenant_id, file.id
    _patch_s3(
        monkeypatch, _csv([_CUATRO[0], ("2026-01-06", "Azúcar", "640"), *_CUATRO[2:]])
    )
    perdida = await _aplicar_frena(db_session, tid, fid)
    assert perdida.to_lose == 1
    assert [c["before"]["amount"] for c in perdida.samples if c["action"] == "lose"] == [
        "800.00"
    ]


async def test_precio_por_cantidad_en_el_apply_frena_solo_por_lo_que_falta(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    from copy import deepcopy

    summary = _summary_precio_por_cantidad(_PxC)
    file = await _subir_e_importar(
        db_session,
        tenant.tenant_id,
        b"pxc",
        _XLSX_MIME,
        "pxc.xlsx",
        summary=deepcopy(summary),
        confirmed={"ventas": True},
        context_mappings={_CTX_PxC: _MAPEO_PxC},
        context_confirmed={_CTX_PxC: True},
    )
    # El apply de una sesión usa el borrador con el mapeo, como en producción.
    borrador = {
        "column_mappings": [
            {"context_id": _CTX_PxC, "source_column": col, "target_field": target}
            for col, target in _MAPEO_PxC.items()
        ],
    }
    fid = file.id
    _patch_s3(monkeypatch, b"pxc")

    run_igual = DataRepairRun(
        tenant_id=tenant.tenant_id,
        repair_type=reread_service.REPAIR_TYPE_REREAD,
        status="APPLYING",
        dry_run=False,
        details_json={"file_id": str(file.id), "draft": borrador},
    )
    db_session.add(run_igual)
    await db_session.flush()
    await reread_service.apply_reread(
        db_session, file.id, tenant.tenant_id, run=run_igual, fresh_override=deepcopy(summary)
    )
    await db_session.commit()
    assert await _vivos(db_session, fid) == (4, 0)


# ── Unidad: el emparejamiento exacto ───────────────────────────────────────────


def _foto(kind: str, amount: str, dia: str, ref: str | None = None) -> dict[str, Any]:
    return {
        "kind": kind,
        "amount": amount,
        "transaction_date": f"{dia}T00:00:00",
        "source_row_ref": ref,
        "notes": None,
        "description": None,
    }


def _items(
    anuladas: list[dict[str, Any]], insertadas: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    return [
        *({"action": reread_service.ACTION_VOID, "before": f, "after": None} for f in anuladas),
        *({"action": reread_service.ACTION_INSERT, "before": None, "after": f} for f in insertadas),
    ]


def test_un_importe_corregido_en_la_misma_fila_es_un_reemplazo() -> None:
    perdida = reread_service.medir_perdida_real(
        _items(
            [_foto("sale", "1000", "2026-05-01", "r1")],
            [_foto("sale", "1200", "2026-05-01", "r1")],
        ),
        [],
        [],
    )
    assert perdida.to_lose == 0


def test_otra_fila_con_otro_importe_no_reemplaza_aunque_sea_el_mismo_dia() -> None:
    perdida = reread_service.medir_perdida_real(
        _items(
            [_foto("sale", "1000", "2026-05-01", "r1")],
            [_foto("sale", "1200", "2026-05-01", "r9")],
        ),
        [],
        [],
    )
    assert perdida.to_lose == 1


def test_el_reemplazo_de_una_fila_de_otros_no_tapa_una_perdida() -> None:
    """Revisión 2 (F3): el insertado que reemplaza un registro clasificado desde
    «Otros» no tiene su anulado en los items; suelto, emparejaba una pérdida."""
    perdida = reread_service.medir_perdida_real(
        _items(
            [_foto("sale", "500", "2026-05-01", "r1")],
            [_foto("sale", "500", "2026-05-01", "r-otros")],
        ),
        [],
        [],
        {"r-otros"},
    )
    assert perdida.to_lose == 1


def test_lo_perdido_que_aparece_en_otros_dice_adonde_va() -> None:
    perdida = reread_service.medir_perdida_real(
        _items([_foto("sale", "7000", "2026-05-01", "r1")], []),
        [],
        [{"detalle": "Devolución proveedor", "dinero_ingreso": "7000"}],
    )
    assert (perdida.to_lose, perdida.to_lose_to_others) == (1, 1)
    assert perdida.samples[0]["after"] == {"destino": "Otros"}


# ── El worker ──────────────────────────────────────────────────────────────────


class _EngineFalso:
    """El worker dispone del engine al terminar; acá no hay nada que disponer."""

    async def dispose(self) -> None:
        return None


async def test_el_worker_frenado_revierte_todo_y_deja_la_sesion_lista_para_confirmar(
    isolated_db_engine: Any, monkeypatch: pytest.MonkeyPatch, mock_score_trigger: Any
) -> None:
    """Base aislada: el worker hace rollback y commits de verdad, y en la conexión
    compartida de la suite eso se llevaría puesto el test entero."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.jobs import reread_worker

    factory = async_sessionmaker(isolated_db_engine, expire_on_commit=False)
    monkeypatch.setattr(
        reread_worker, "_build_async_session", lambda _url: (_EngineFalso(), factory)
    )
    tid = uuid.uuid4()
    async with factory() as s:
        s.add(
            Tenant(
                tenant_id=tid,
                legal_name="Deco Aislado",
                display_name="Deco Aislado",
                currency="ARS",
                pricing_reference_mode="MEP",
                status="ACTIVE",
            )
        )
        await s.flush()
        await add_business_profile(s, tid)
        await s.commit()
        file = await _subir_e_importar(s, tid, _csv(_CUATRO), "text/csv", "gastos.csv")
        fid = file.id
    _patch_s3(monkeypatch, _csv(_CUATRO[:3]))

    async def _encolar(aceptar: bool) -> uuid.UUID:
        async with factory() as s:
            run, fresh = await reread_service.start_or_resume_preview_session(s, fid, tid)
            await reread_service.preview_reread(s, fid, tid, fresh_override=fresh, run=run)
            reread_service.mark_session_ready_to_apply(run)
            run.details_json = {**(run.details_json or {}), "accept_data_loss": aceptar}
            await s.flush()
            run = await reread_service.start_background_apply(s, fid, tid, existing_run=run)
            await s.commit()
            return run.id

    run_id = await _encolar(aceptar=False)
    salida = await reread_worker.aplicar_relectura(str(run_id), str(fid), str(tid))
    assert salida["status"] == "NEEDS_CONFIRMATION"
    async with factory() as s:
        assert await _vivos(s, fid) == (0, 4), "frenado = nada aplicado"
        run = await s.get(DataRepairRun, run_id)
        assert run is not None and run.status == "READY_TO_APPLY"
        assert run.completed_at is None
        details = run.details_json or {}
        assert details["data_loss"]["to_lose"] == 1
        assert "accept_data_loss" not in details
        # La sesión sigue entera: aceptar aplica la MISMA lectura, sin S3.
        assert details.get("fresh_summary"), "el resumen cacheado de la sesión"

    # Aceptar con un tope MENOR al medido (la base cambió, o se aceptó otra cosa)
    # no alcanza: vuelve a frenar.
    async with factory() as s:
        run = await s.get(DataRepairRun, run_id)
        assert run is not None
        run.details_json = {
            **(run.details_json or {}),
            "accept_data_loss": True,
            "data_loss": {**(run.details_json or {})["data_loss"], "to_lose": 0},
        }
        await s.flush()
        await reread_service.start_background_apply(s, fid, tid, existing_run=run)
        await s.commit()
    salida = await reread_worker.aplicar_relectura(str(run_id), str(fid), str(tid))
    assert salida["status"] == "NEEDS_CONFIRMATION"
    async with factory() as s:
        assert await _vivos(s, fid) == (0, 4)

    # Confirmado: la MISMA sesión se vuelve a encolar con la aceptación.
    async with factory() as s:
        run = await s.get(DataRepairRun, run_id)
        assert run is not None
        version = int((run.details_json or {}).get("draft_version", 0))
        await reread_service.validate_ready_to_apply(s, run_id, tid, fid, version)
        run.details_json = {**(run.details_json or {}), "accept_data_loss": True}
        await s.flush()
        await reread_service.start_background_apply(s, fid, tid, existing_run=run)
        await s.commit()
    salida = await reread_worker.aplicar_relectura(str(run_id), str(fid), str(tid))
    assert salida["status"] == "APPLIED"
    async with factory() as s:
        assert await _vivos(s, fid) == (0, 3)


# ── La pantalla: el apply y el estado por HTTP ─────────────────────────────────


async def test_el_estado_del_run_frenado_dice_que_se_pierde_y_el_apply_acepta(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.jobs import reread_worker

    file = await _subir_e_importar(
        db_session, sample_tenant.tenant_id, _csv(_CUATRO), "text/csv", "gastos.csv"
    )
    fid = file.id
    _patch_s3(monkeypatch, _csv(_CUATRO[:3]))
    pre = await client.post(f"/api/v1/ingestion/files/{fid}/reread/preview", headers=auth_headers)
    assert pre.status_code == 200, pre.text
    body = pre.json()
    assert body["counts"]["to_lose"] == 1  # el estimado, informativo
    pedido = {"run_id": body["run_id"], "draft_version": body["draft_version"]}
    run_id = uuid.UUID(body["run_id"])
    sin_worker = unittest.mock.patch(
        "app.application.services.reread_diagnostics_service.check_ingestion_workers_available",
        return_value=None,
    )

    with sin_worker, unittest.mock.patch.object(reread_worker.reread_apply, "delay") as delay:
        r = await client.post(
            f"/api/v1/ingestion/files/{fid}/reread/apply", headers=auth_headers, json=pedido
        )
        assert r.status_code == 202, r.text
        delay.assert_called_once()
    run = await db_session.get(DataRepairRun, run_id)
    assert run is not None
    await db_session.refresh(run)
    assert (run.details_json or {})["accept_data_loss"] is False

    # Lo que deja el worker al frenar (probado aparte, contra una base aislada).
    reread_service.volver_a_confirmar(
        run, reread_service.PerdidaDeRelectura(to_lose=1, samples=[])
    )
    await db_session.commit()

    # Retomar la sesión (recargar la página) lleva directo a la medición.
    retomado = await client.post(
        f"/api/v1/ingestion/files/{fid}/reread/preview", headers=auth_headers
    )
    assert retomado.status_code == 200, retomado.text
    assert retomado.json()["run_id"] == body["run_id"]
    assert retomado.json()["data_loss"]["to_lose"] == 1

    estado = await client.get(
        f"/api/v1/ingestion/files/{fid}/reread/runs/{run_id}", headers=auth_headers
    )
    assert estado.status_code == 200, estado.text
    e = estado.json()
    # FAILED para un frontend viejo: deja de consultar y muestra `error`.
    assert e["status"] == "FAILED"
    assert e["error_code"] == "REREAD_WOULD_LOSE_DATA"
    assert e["data_loss"]["to_lose"] == 1
    assert "no se aplicó" in e["error"]
    assert e["draft_version"] == body["draft_version"]

    with sin_worker, unittest.mock.patch.object(reread_worker.reread_apply, "delay") as delay:
        r = await client.post(
            f"/api/v1/ingestion/files/{fid}/reread/apply",
            headers=auth_headers,
            json={**pedido, "accept_data_loss": True},
        )
        assert r.status_code == 202, r.text
        delay.assert_called_once()
    await db_session.refresh(run)
    assert (run.details_json or {})["accept_data_loss"] is True
