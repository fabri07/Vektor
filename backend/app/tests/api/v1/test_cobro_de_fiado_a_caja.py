"""«Cobro saldo fiado» no es una venta: es plata que entra a la caja y baja la deuda del
cliente. Con el cliente identificado entra como cobro (`inflow`); sin él va a «Otros» con
su motivo y se resuelve eligiendo el cliente, nunca como «venta sin producto»."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import customer_alias_service as alias
from app.application.services.ingestion_import_service import (
    COBRO_DE_FIADO_KEY,
    MOTIVO_COBRO_SIN_CLIENTE,
    insert_confirmed_data,
)
from app.domain.business_time import today_ar
from app.persistence.models.customer import Customer
from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.models.unclassified_record import UnclassifiedRecord
from app.tests._respaldo import producto_con_compra

pytestmark = pytest.mark.asyncio

HACE_3 = (today_ar() - timedelta(days=3)).isoformat()


@pytest.fixture(autouse=True)
def _sin_celery(mock_score_trigger: Any) -> None:
    pass


async def _cliente(db: AsyncSession, tid: uuid.UUID, nombre: str, apellido: str | None) -> Customer:
    c = Customer(tenant_id=tid, name=nombre, last_name=apellido)
    db.add(c)
    await db.commit()
    return c


async def _importar(
    db: AsyncSession, tid: uuid.UUID, filas: list[dict[str, Any]]
) -> dict[str, Any]:
    f = UploadedFile(
        id=uuid.uuid4(), tenant_id=tid, uploaded_by=None, original_filename="ventas.xlsx",
        s3_key="ventas.xlsx", content_type="application/vnd.ms-excel", size_bytes=100,
        purpose="ventas", status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        parsed_summary_json={
            "confidence": "HIGH", "file_type": "spreadsheet", "inferred_type": "ventas",
            "has_venta": True, "row_count": len(filas), "ventas_detectadas": filas,
            "headers": list(filas[0]), "preview_rows": filas,
        },
    )
    db.add(f)
    await db.commit()
    counts = await insert_confirmed_data(
        db, tid, f.parsed_summary_json or {}, {"ventas": True},
        uploaded_file_id=f.id, column_mappings={"cliente": "customer_name", "detalle": "notes"},
    )
    await db.commit()
    return counts


def _cobro(cliente: str, monto: str = "30000", pago: str = "Efectivo",
           detalle: str = "Cobro saldo fiado") -> dict[str, Any]:
    return {"fecha": HACE_3, "venta": monto, "detalle": detalle, "cliente": cliente,
            "medio_pago": pago, "__context__": "table"}


async def _ventas(db: AsyncSession, tid: uuid.UUID) -> list[SaleEntry]:
    return list(
        (
            await db.execute(
                select(SaleEntry).where(SaleEntry.tenant_id == tid, SaleEntry.voided_at.is_(None))
            )
        ).scalars()
    )


async def _otros(db: AsyncSession, tid: uuid.UUID) -> list[UnclassifiedRecord]:
    return list(
        (
            await db.execute(
                select(UnclassifiedRecord).where(UnclassifiedRecord.tenant_id == tid)
            )
        ).scalars()
    )


async def test_el_cobro_con_cliente_identificado_entra_como_cobro_y_no_como_venta(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    caro = await _cliente(db_session, tid, "Caro", "G.")

    counts = await _importar(db_session, tid, [_cobro("caro g.")])

    (cobro,) = await _ventas(db_session, tid)
    assert (cobro.payment_method, cobro.customer_id, cobro.amount) == (
        "inflow", caro.id, Decimal("30000.00")
    )
    assert cobro.product_id is None
    assert (cobro.custom_fields or {}).get("medio_del_cobro") == "cash"
    assert counts["cobros_de_fiado"] == 1
    assert counts.get("ventas", 0) == 0
    assert await _otros(db_session, tid) == []  # ya no es «Venta sin producto»


async def test_el_alias_confirmado_identifica_al_cliente_del_cobro(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    caro = await _cliente(db_session, tid, "Carolina", "Giménez")
    await alias.aprender_alias(
        db_session, tenant_id=tid, user_id=None, texto="carolina g", customer_id=caro.id
    )
    await db_session.commit()

    await _importar(db_session, tid, [_cobro("carolina g", "20000", "Transferencia")])

    (cobro,) = await _ventas(db_session, tid)
    assert (cobro.payment_method, cobro.customer_id) == ("inflow", caro.id)
    assert (cobro.custom_fields or {}).get("medio_del_cobro") == "transfer"


async def test_sin_cliente_real_va_a_otros_con_su_motivo_y_no_pide_producto(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id

    counts = await _importar(db_session, tid, [_cobro("Caro G.")])

    assert await _ventas(db_session, tid) == []
    (fila,) = await _otros(db_session, tid)
    assert fila.context_label == MOTIVO_COBRO_SIN_CLIENTE
    assert fila.suggested_entity == "cobro"
    assert fila.row_data.get(COBRO_DE_FIADO_KEY) == "1"
    assert fila.row_data.get("cliente") == "Caro G."
    assert counts["cobros_sin_cliente"] == 1


async def test_un_cobro_a_secas_o_con_producto_sigue_siendo_una_venta(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    await producto_con_compra(db_session, tid, nombre="Yerba")
    await _cliente(db_session, tid, "Caro", "G.")

    counts = await _importar(
        db_session, tid, [_cobro("caro g.", detalle="Cobro mostrador"),
                          _cobro("caro g.", detalle="Venta fiada", pago="Efectivo")],
    )

    assert counts.get("cobros_de_fiado", 0) == 0
    assert all(v.payment_method != "inflow" for v in await _ventas(db_session, tid))


async def test_resolver_el_cobro_desde_otros_crea_el_cobro_y_aprende_el_alias(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    caro = await _cliente(db_session, tid, "Carolina", "Giménez")
    await _importar(db_session, tid, [_cobro("Caro G.")])
    (fila,) = await _otros(db_session, tid)

    r = await client.post(
        f"/api/v1/others/{fila.id}/reclassify",
        json={
            "entity_type": "cobro",
            "fields": {
                "amount": 30000,
                "transaction_date": f"{HACE_3}T00:00:00",
                "payment_method": "cash",
                "customer_id": str(caro.id),
            },
        },
        headers=auth_headers,
    )

    assert r.status_code == 200, r.text
    (cobro,) = await _ventas(db_session, tid)
    assert (cobro.payment_method, cobro.customer_id) == ("inflow", caro.id)
    assert cobro.product_id is None
    await db_session.refresh(fila)
    assert fila.status == "IMPORTED"
    assert await alias.cliente_por_alias(db_session, tid, "caro g.") == caro.id


async def test_un_cobro_sin_cliente_real_no_se_resuelve(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    await _importar(db_session, tid, [_cobro("Caro G.")])
    (fila,) = await _otros(db_session, tid)

    r = await client.post(
        f"/api/v1/others/{fila.id}/reclassify",
        json={
            "entity_type": "cobro",
            "fields": {
                "amount": 30000, "transaction_date": f"{HACE_3}T00:00:00",
                "payment_method": "cash",
            },
        },
        headers=auth_headers,
    )

    assert r.status_code == 400, r.text
    assert await _ventas(db_session, tid) == []
    await db_session.refresh(fila)
    assert fila.status == "PENDING"


async def test_importar_los_sugeridos_no_toca_un_cobro_pendiente(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    await _importar(db_session, tid, [_cobro("Caro G.")])

    r = await client.post("/api/v1/others/bulk-import", json={"entity_type": "sale"},
                          headers=auth_headers)

    assert r.status_code == 200, r.text
    assert await _ventas(db_session, tid) == []
    (fila,) = await _otros(db_session, tid)
    assert fila.status == "PENDING"

