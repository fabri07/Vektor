"""«Pago a proveedor»: movimiento propio, no gasto ni compra.

Saca plata de la caja y baja la deuda con el proveedor; no toca el costo (la compra fiada
ya contó). Una hoja de gastos que lo trae como una fila más no puede contarlo dos veces:
va a «Otros» para elegir el proveedor y de ahí es un pago, no un gasto."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import supplier_payment_service as pagos
from app.application.services.cash_close_service import CashCloseService
from app.application.services.file_deletion_service import (
    preview_file_deletion,
    revert_file_data,
)
from app.application.services.ingestion_import_service import (
    ENTIDAD_PAGO_PROVEEDOR,
    MOTIVO_PAGO_A_PROVEEDOR,
    insert_confirmed_data,
)
from app.domain.business_time import today_ar
from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
from app.persistence.models.supplier import Supplier
from app.persistence.models.supplier_payment import SupplierPayment
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.persistence.models.unclassified_record import UnclassifiedRecord
from app.state.business_state_service import compute_business_state

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("mock_score_trigger")]

HOY = today_ar()
HACE_3 = (HOY - timedelta(days=3)).isoformat()


async def _proveedor(db: AsyncSession, tid: uuid.UUID, nombre: str = "Textil Sur") -> Supplier:
    p = Supplier(tenant_id=tid, name=nombre)
    db.add(p)
    await db.commit()
    return p


async def _compra_fiada(
    db: AsyncSession, tid: uuid.UUID, proveedor: Supplier, monto: str = "100000"
) -> None:
    db.add(
        ExpenseEntry(
            tenant_id=tid,
            supplier_id=proveedor.id,
            amount=Decimal(monto),
            category="INVENTORY",
            transaction_date=datetime.now() - timedelta(days=20),
            description="Compra fiada",
            payment_method="account",
            provenance="REAL",
        )
    )
    await db.commit()


def _pago(monto: str = "40000", medio: str = "cash", **extra: Any) -> dict[str, Any]:
    return {"amount": monto, "occurred_on": HOY.isoformat(), "payment_method": medio, **extra}


async def test_el_pago_baja_la_deuda_y_anularlo_la_devuelve(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    prov = await _proveedor(db_session, sample_tenant.tenant_id)
    await _compra_fiada(db_session, sample_tenant.tenant_id, prov)
    base = f"/api/v1/suppliers/{prov.id}"

    alta = await client.post(f"{base}/payments", json=_pago(), headers=auth_headers)
    assert alta.status_code == 201, alta.text
    saldo = (await client.get(f"{base}/balance", headers=auth_headers)).json()
    assert (saldo["comprado_a_cuenta_corriente"], saldo["pagado"], saldo["deuda"]) == (
        "100000.00", "40000.00", "60000.00"
    )
    # No es un gasto: la tabla de gastos sigue con la sola compra fiada.
    assert await db_session.scalar(select(func.count()).select_from(ExpenseEntry)) == 1

    anulado = await client.post(
        f"{base}/payments/{alta.json()['id']}/void", json={"reason": "se cargó dos veces"},
        headers=auth_headers,
    )
    assert anulado.status_code == 200 and anulado.json()["voided_at"] is not None
    saldo = (await client.get(f"{base}/balance", headers=auth_headers)).json()
    assert saldo["deuda"] == "100000.00"
    otra = await client.post(
        f"{base}/payments/{alta.json()['id']}/void", json={"reason": "otra vez"},
        headers=auth_headers,
    )
    assert otra.status_code == 409


async def test_validaciones_del_alta(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    prov = await _proveedor(db_session, sample_tenant.tenant_id)
    url = f"/api/v1/suppliers/{prov.id}/payments"
    assert (await client.post(url, json=_pago("0"), headers=auth_headers)).status_code == 422
    assert (await client.post(url, json=_pago(medio="account"), headers=auth_headers)
            ).status_code == 422  # pagar «a cuenta corriente» no saca plata de nada
    futuro = _pago(occurred_on=(HOY + timedelta(days=2)).isoformat())
    assert (await client.post(url, json=futuro, headers=auth_headers)).status_code == 422
    ajeno = f"/api/v1/suppliers/{uuid.uuid4()}/payments"
    assert (await client.post(ajeno, json=_pago(), headers=auth_headers)).status_code == 404


async def test_el_pago_en_efectivo_sale_del_arqueo_y_el_de_transferencia_no(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    prov = await _proveedor(db_session, tid)
    db_session.add(SaleEntry(tenant_id=tid, amount=Decimal("80000"),
                             transaction_date=datetime.combine(HOY, datetime.min.time()),
                             payment_method="cash"))
    await db_session.commit()
    svc = CashCloseService(db_session)
    assert await svc._compute_expected(tid, HOY) == (80000.0, {"cash": 80000.0})

    await pagos.registrar_pago(db_session, tenant_id=tid, user_id=None, supplier_id=prov.id,
                               amount=Decimal("30000"), occurred_on=HOY, payment_method="cash")
    await pagos.registrar_pago(db_session, tenant_id=tid, user_id=None, supplier_id=prov.id,
                               amount=Decimal("7000"), occurred_on=HOY,
                               payment_method="transfer")
    await db_session.commit()
    assert await svc._compute_expected(tid, HOY) == (50000.0, {"cash": 50000.0})


async def test_el_pago_sale_de_la_liquidez_sin_ser_gasto(
    db_session: AsyncSession, sample_tenant: Tenant, fake_redis: Any
) -> None:
    tid = sample_tenant.tenant_id
    prov = await _proveedor(db_session, tid)
    antes = await compute_business_state(tid, db_session, fake_redis)
    await pagos.registrar_pago(
        db_session, tenant_id=tid, user_id=None, supplier_id=prov.id, amount=Decimal("9000"),
        occurred_on=HOY - timedelta(days=2), payment_method="transfer",
    )
    await db_session.commit()
    despues = await compute_business_state(tid, db_session, fake_redis)
    assert despues.liquid_outflow_est - antes.liquid_outflow_est == Decimal("9000")
    assert despues.monthly_fixed_expenses_est == antes.monthly_fixed_expenses_est  # no es un gasto


# ── Importación: «Pago a proveedor» no se cuenta como gasto ─────────────────────────


async def _importar_gastos(
    db: AsyncSession, tid: uuid.UUID, filas: list[dict[str, Any]]
) -> tuple[UploadedFile, dict[str, Any]]:
    f = UploadedFile(
        id=uuid.uuid4(), tenant_id=tid, uploaded_by=None, original_filename="gastos.xlsx",
        s3_key="gastos.xlsx", content_type="application/vnd.ms-excel", size_bytes=100,
        purpose="gastos", status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        parsed_summary_json={
            "file_type": "spreadsheet", "inferred_type": "gastos", "row_count": len(filas),
            "gastos_detectados": filas, "ventas_detectadas": [], "stock_detectado": [],
        },
    )
    db.add(f)
    await db.commit()
    counts = await insert_confirmed_data(
        db, tid, f.parsed_summary_json or {}, {"gastos": True}, uploaded_file_id=f.id
    )
    await db.commit()
    return f, counts


def _fila(concepto: str, categoria: str, monto: str = "50000", pago: str = "transferencia"
          ) -> dict[str, Any]:
    return {"fecha": HACE_3, "concepto": concepto, "categoria": categoria, "monto": monto,
            "forma_pago": pago}


async def _gastos(db: AsyncSession, tid: uuid.UUID) -> list[ExpenseEntry]:
    return list((await db.execute(
        select(ExpenseEntry).where(ExpenseEntry.tenant_id == tid,
                                   ExpenseEntry.voided_at.is_(None))
    )).scalars())


async def test_una_fila_pago_a_proveedor_va_a_otros_y_no_cuenta_como_gasto(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    _, counts = await _importar_gastos(db_session, tid, [
        _fila("Pago a proveedor", "Pago a proveedor"),
        _fila("Alquiler local", "Alquiler", "300000"),
    ])

    assert [g.description for g in await _gastos(db_session, tid)] == ["Alquiler local"]
    (otro,) = (await db_session.execute(select(UnclassifiedRecord))).scalars().all()
    assert otro.context_label == MOTIVO_PAGO_A_PROVEEDOR[:200]
    assert otro.suggested_entity == ENTIDAD_PAGO_PROVEEDOR
    assert counts["pagos_a_proveedor_a_otros"] == 1
    assert counts["gastos"] == 1


async def test_un_gasto_comun_y_una_compra_con_proveedor_siguen_siendo_gasto(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    await _importar_gastos(db_session, tid, [
        _fila("Pago de alquiler", "Alquiler"),
        _fila("Proveedor de packaging", "Packaging"),
        _fila("Pago luz", "Servicios"),
    ])
    assert len(await _gastos(db_session, tid)) == 3
    assert (await db_session.execute(select(UnclassifiedRecord))).scalars().all() == []


async def test_resolver_la_fila_crea_el_pago_y_borrar_el_archivo_lo_anula(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    prov = await _proveedor(db_session, tid)
    await _compra_fiada(db_session, tid, prov)
    archivo, _ = await _importar_gastos(db_session, tid, [
        _fila("Pago a proveedor", "Pago a proveedor", "100000")
    ])
    (otro,) = (await db_session.execute(select(UnclassifiedRecord))).scalars().all()

    resp = await client.post(
        f"/api/v1/others/{otro.id}/reclassify",
        json={"entity_type": "pago_proveedor", "fields": {
            "supplier_id": str(prov.id), "amount": "100000", "occurred_on": HACE_3,
            "payment_method": "transfer"}},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    (pago,) = (await db_session.execute(select(SupplierPayment))).scalars().all()
    assert (pago.source_type, pago.source_upload_id) == ("archivo", archivo.id)
    saldo = await pagos.saldo_de_proveedor(db_session, tid, prov.id)
    assert saldo.deuda == Decimal("0")  # la compra fiada + su pago = una vez, no dos
    assert [g.amount for g in await _gastos(db_session, tid)] == [Decimal("100000.00")]

    vista = await preview_file_deletion(db_session, archivo.id, tid)
    assert vista["pagos_a_proveedores"] == 1
    out = await revert_file_data(db_session, archivo.id, tid)
    await db_session.commit()
    assert out["pagos_a_proveedores"] == 1
    await db_session.refresh(pago)
    assert pago.voided_at is not None
    saldo = await pagos.saldo_de_proveedor(db_session, tid, prov.id)
    assert saldo.deuda == Decimal("100000")


async def test_resolver_con_un_proveedor_inexistente_es_422(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    await _importar_gastos(db_session, tid, [_fila("Pago a proveedor", "Pago a proveedor")])
    (otro,) = (await db_session.execute(select(UnclassifiedRecord))).scalars().all()
    resp = await client.post(
        f"/api/v1/others/{otro.id}/reclassify",
        json={"entity_type": "pago_proveedor", "fields": {
            "supplier_id": str(uuid.uuid4()), "amount": "50000", "occurred_on": HACE_3,
            "payment_method": "transfer"}},
        headers=auth_headers,
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "INVALID_SUPPLIER"
