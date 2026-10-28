"""I4-6 + I1-4, revisión adversarial del PR #221.

**Grave.** La huella es de la FILA, no de la captura: en la tabla suelta una fila
puede dejar un gasto en los libros y una venta en «Otros» con la misma ref, o dos
capturas (la venta sin producto y la compra sin artículos). Liberar la huella del
pendiente reimporta la fila entera, así que releer sin cambios resucitaba el
gasto que el usuario había anulado a mano, o devolvía a la bandeja la venta que
había descartado.

**Medio.** El preview restaba las huellas de los pendientes y las contaba como
nuevas, pero no reproduce el ruteo a «Otros»: una relectura sin cambios
anunciaba ventas nuevas que el apply volvía a mandar a «Otros». Ahora van
aparte (``retried_from_others``), con las mismas exclusiones que el apply.

**Menor 1.** Una captura de riesgo pendiente guarda la ref de la fila siguiente
(I1-4): no es «copia nueva» del pendiente de esa fila.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import reread_service
from app.application.services.file_parsing import parse_uploaded_content
from app.application.services.ingestion_import_service import (
    RISK_REF_KEY,
    ROW_REF_KEY,
    huellas_presentes,
    insert_confirmed_data,
)
from app.persistence.models.file import PROCESSING_STATUS_DONE, UploadedFile
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.unclassified_record import (
    UNCLASSIFIED_STATUS_DISMISSED,
    UNCLASSIFIED_STATUS_PENDING,
    UnclassifiedRecord,
)
from app.tests.services import test_relectura_relee_otros_pendientes as _base
from app.tests.services.test_relectura_relee_otros_pendientes import (
    _CSV_CORREGIDO,
    _archivo_de_ventas,
    _confirmar_con_riesgo,
    _gastos_vivos,
    _hoja,
    _otros,
    _servir,
    _servir_summary,
    _ventas_vivas,
)

# Las fixtures del archivo base (sin broker, negocio y producto con compra).
_sin_broker = _base._sin_broker
tenant = _base.tenant
producto = _base.producto

#: Una fila con venta y gasto: el gasto entra y la venta (sin producto) va a «Otros».
_VENTA_Y_GASTO = b"fecha,detalle,venta,gasto\n2026-01-05,A,1500,900\n"
#: Con la categoría de mercadería: la compra sin artículos también va a «Otros».
_VENTA_Y_COMPRA = b"fecha,detalle,venta,gasto,categoria\n2026-01-05,A,1500,900,mercaderia\n"


async def _libro(
    session: AsyncSession,
    tenant: Tenant,
    monkeypatch: pytest.MonkeyPatch,
    contenido: bytes,
) -> UploadedFile:
    _servir(monkeypatch, contenido)
    summary = parse_uploaded_content(contenido, "text/csv", "libro.csv")
    confirmados = {"ventas": True, "gastos": True}
    archivo = UploadedFile(
        id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        uploaded_by=None,
        original_filename="libro.csv",
        s3_key=f"tenants/{tenant.tenant_id}/libro.csv",
        content_type="text/csv",
        size_bytes=len(contenido),
        purpose="ventas",
        processing_status=PROCESSING_STATUS_DONE,
        parsed_summary_json={
            "inferred_type": summary.get("inferred_type"),
            "confirmed_fields": confirmados,
        },
    )
    session.add(archivo)
    await session.commit()
    await insert_confirmed_data(
        session, tenant.tenant_id, summary, confirmados,
        source="ingestion", uploaded_file_id=archivo.id,
    )
    await session.commit()
    return archivo


# ── Grave: lo resuelto a mano sobre un hermano no resucita ──────────────────────


async def test_el_gasto_anulado_a_mano_no_resucita_por_su_venta_pendiente(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    archivo = await _libro(db_session, tenant, monkeypatch, _VENTA_Y_GASTO)
    gastos = await _gastos_vivos(db_session, tenant)
    assert [Decimal(g.amount) for g in gastos] == [Decimal("900")]
    pendiente = (await _otros(db_session, tenant, archivo.id, UNCLASSIFIED_STATUS_PENDING))[0]
    assert pendiente.suggested_entity == "sale"
    ref = str(pendiente.row_data[ROW_REF_KEY])
    # Lo que hace `DELETE /expenses/{id}`: anula sin `has_user_edits`.
    gastos[0].voided_at = datetime.now(UTC)
    gastos[0].void_reason = "MANUAL_ADMIN_VOID"
    await db_session.commit()

    preview = await reread_service.preview_reread(db_session, archivo.id, tenant.tenant_id)
    assert preview.retried_from_others == 0, "el preview promete releer la fila anulada"

    await reread_service.apply_reread(db_session, archivo.id, tenant.tenant_id)
    await db_session.commit()

    assert await _gastos_vivos(db_session, tenant) == [], "resucitó el gasto anulado a mano"
    quedan = await _otros(db_session, tenant, archivo.id, UNCLASSIFIED_STATUS_PENDING)
    assert [p.id for p in quedan] == [pendiente.id]
    assert await huellas_presentes(db_session, tenant.tenant_id, {ref}) == {ref}


async def test_el_descartado_a_mano_no_vuelve_a_la_bandeja_por_su_hermano_pendiente(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    archivo = await _libro(db_session, tenant, monkeypatch, _VENTA_Y_COMPRA)
    pendientes = await _otros(db_session, tenant, archivo.id, UNCLASSIFIED_STATUS_PENDING)
    assert sorted(p.suggested_entity or "" for p in pendientes) == ["expense", "sale"]
    venta = next(p for p in pendientes if p.suggested_entity == "sale")
    # Lo que hace `POST /others/{id}/dismiss`.
    venta.status = UNCLASSIFIED_STATUS_DISMISSED
    venta.resolved_at = datetime.now(UTC)
    await db_session.commit()

    await reread_service.apply_reread(db_session, archivo.id, tenant.tenant_id)
    await db_session.commit()

    quedan = await _otros(db_session, tenant, archivo.id, UNCLASSIFIED_STATUS_PENDING)
    assert [p.suggested_entity for p in quedan] == ["expense"], [
        (p.suggested_entity, p.context_label) for p in quedan
    ]
    descartada = await db_session.get(UnclassifiedRecord, venta.id)
    assert descartada is not None and descartada.status == UNCLASSIFIED_STATUS_DISMISSED


async def test_lo_que_descarto_una_relectura_no_traba_la_siguiente(
    db_session: AsyncSession,
    tenant: Tenant,
    producto: Product,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La primera relectura sin cambios deja DISMISSED («releida») la copia vieja
    y pendiente la nueva, con la misma ref. Ese descarte no es del usuario: la
    segunda relectura, con el archivo corregido, carga la venta."""
    archivo = await _archivo_de_ventas(db_session, tenant, monkeypatch)
    await reread_service.apply_reread(db_session, archivo.id, tenant.tenant_id)
    await db_session.commit()
    assert len(await _otros(db_session, tenant, archivo.id, UNCLASSIFIED_STATUS_DISMISSED)) == 1
    assert len(await _otros(db_session, tenant, archivo.id, UNCLASSIFIED_STATUS_PENDING)) == 1

    _servir(monkeypatch, _CSV_CORREGIDO)
    await reread_service.apply_reread(db_session, archivo.id, tenant.tenant_id)
    await db_session.commit()

    assert len(await _ventas_vivas(db_session, tenant)) == 2
    assert await _otros(db_session, tenant, archivo.id, UNCLASSIFIED_STATUS_PENDING) == []


# ── Medio: el preview no promete lo que sigue ilegible ──────────────────────────


async def test_el_preview_sin_cambios_no_promete_la_fila_que_sigue_ilegible(
    db_session: AsyncSession,
    tenant: Tenant,
    producto: Product,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archivo = await _archivo_de_ventas(db_session, tenant, monkeypatch)

    preview = await reread_service.preview_reread(db_session, archivo.id, tenant.tenant_id)
    resultado = await reread_service.apply_reread(db_session, archivo.id, tenant.tenant_id)
    await db_session.commit()

    assert resultado.new == 0
    assert preview.new == resultado.new, (preview.new, resultado.new)
    assert preview.retried_from_others == 1
    assert preview.counts()["retried_from_others"] == 1
    assert len(await _otros(db_session, tenant, archivo.id, UNCLASSIFIED_STATUS_PENDING)) == 1


# ── Menor 1: la captura de riesgo no es copia del pendiente de la fila siguiente ─


async def test_la_captura_de_riesgo_no_descarta_el_pendiente_de_la_fila_siguiente(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B (sin monto) va a «Otros» por F8 y su captura guarda la ref de C; C (fecha
    ilegible) cae a «Otros» por el import con esa misma ref. Si C ya no está en
    el archivo, nada la toma: tiene que quedar pendiente y con su huella, no
    descartada como «releida» por la captura de riesgo."""
    confirmados = {"ventas": False, "gastos": True}
    archivo = await _confirmar_con_riesgo(
        db_session, tenant, _hoja(["1000", "", "500"], fecha_c="cuando pueda"), confirmados
    )
    pendientes = await _otros(db_session, tenant, archivo.id, UNCLASSIFIED_STATUS_PENDING)
    riesgo = [p for p in pendientes if (p.row_data or {}).get(RISK_REF_KEY)]
    filas_c = [p for p in pendientes if not (p.row_data or {}).get(RISK_REF_KEY)]
    assert len(riesgo) == 1 and len(filas_c) == 1, [p.row_data for p in pendientes]
    ref_c = str(filas_c[0].row_data[ROW_REF_KEY])
    assert str(riesgo[0].row_data[ROW_REF_KEY]) == ref_c

    sin_c: dict[str, Any] = _hoja(["1000", "", "500"], fecha_c="cuando pueda")
    sin_c["gastos_detectados"] = sin_c["gastos_detectados"][:2]
    sin_c["mapping_contexts"][0]["row_count"] = 2
    _servir_summary(monkeypatch, sin_c)
    await reread_service.apply_reread(db_session, archivo.id, tenant.tenant_id)
    await db_session.commit()

    quedan = await _otros(db_session, tenant, archivo.id, UNCLASSIFIED_STATUS_PENDING)
    assert filas_c[0].id in {p.id for p in quedan}, "se descartó C como «releida»"
    assert await huellas_presentes(db_session, tenant.tenant_id, {ref_c}) == {ref_c}
