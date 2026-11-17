"""Combos desde «Otros» («Jean + buzo» $41.000): el servidor propone qué productos suman
el monto y registra UNA venta por línea, atómicas, agrupadas y con la procedencia de la
fila. Sin respaldo, sin stock o con una suma que no cierra no se escribe nada."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.business_time import today_ar
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.models.unclassified_record import UnclassifiedRecord
from app.tests._respaldo import producto_con_compra

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _sin_celery(mock_score_trigger: Any) -> None:
    pass


async def _fila(
    db: AsyncSession, tenant: Tenant, *, detalle: str = "Jean + buzo", monto: str = "41000",
    fecha: str = "2026-10-22", medio: str = "cash",
) -> UnclassifiedRecord:
    rec = UnclassifiedRecord(
        tenant_id=tenant.tenant_id,
        source="ingestion",
        context_label="Venta sin producto",
        row_data={"fecha": fecha, "monto": monto, "detalle": detalle, "medio_de_pago": medio},
        suggested_entity="sale",
        status="PENDING",
    )
    db.add(rec)
    await db.commit()
    return rec


async def _jean_y_buzo(db: AsyncSession, tenant: Tenant) -> tuple[Product, Product, Product]:
    tid = tenant.tenant_id
    jean = await producto_con_compra(db, tid, nombre="Jeans azules T42", precio=Decimal("21000"))
    jean40 = await producto_con_compra(db, tid, nombre="Jean azul T40", precio=Decimal("26000"))
    buzo = await producto_con_compra(
        db, tid, nombre="buzo canguro gris L", precio=Decimal("20000")
    )
    await db.commit()
    return jean, jean40, buzo


def _items(*pares: tuple[Product, int, str]) -> list[dict[str, Any]]:
    return [
        {"product_id": str(p.id), "quantity": q, "unit_price": precio} for p, q, precio in pares
    ]


async def _reclasificar(
    client: AsyncClient, h: dict[str, Any], rec: UnclassifiedRecord, items: list[dict[str, Any]],
    *, monto: int = 41000, medio: str = "cash", **extra: Any,
) -> Any:
    return await client.post(
        f"/api/v1/others/{rec.id}/reclassify",
        json={
            "entity_type": "sale",
            "fields": {
                "amount": monto,
                "transaction_date": "2026-10-22T00:00:00",
                "payment_method": medio,
            },
            "items": items,
            **extra,
        },
        headers=h,
    )


async def _sugerencias(client: AsyncClient, h: dict[str, Any], rec: UnclassifiedRecord) -> Any:
    return await client.get(f"/api/v1/others/{rec.id}/combo-suggestions", headers=h)


async def _ventas(db: AsyncSession, tenant: Tenant) -> list[SaleEntry]:
    return list(
        (await db.execute(select(SaleEntry).where(SaleEntry.tenant_id == tenant.tenant_id)))
        .scalars()
        .all()
    )


async def test_sugiere_la_unica_combinacion_que_suma_el_monto(
    client: AsyncClient, auth_headers: dict[str, Any], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    jean, _jean40, buzo = await _jean_y_buzo(db_session, sample_tenant)
    rec = await _fila(db_session, sample_tenant)

    r = await _sugerencias(client, auth_headers, rec)

    assert r.status_code == 200, r.text
    d = r.json()
    assert d["references"] == ["Jean", "buzo"]
    # 21.000 (T42) + 20.000 (buzo) = 41.000; el T40 daría 46.000.
    assert len(d["solutions"]) == 1
    assert [(x["product_id"], x["quantity"]) for x in d["solutions"][0]] == [
        (str(jean.id), 1), (str(buzo.id), 1),
    ]


async def test_sin_combinacion_exacta_no_propone_nada(
    client: AsyncClient, auth_headers: dict[str, Any], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    await _jean_y_buzo(db_session, sample_tenant)
    rec = await _fila(db_session, sample_tenant, monto="41500")

    d = (await _sugerencias(client, auth_headers, rec)).json()

    assert d["solutions"] == []
    assert "Ninguna combinación suma" in d["reason"]


async def test_un_producto_sin_respaldo_a_la_fecha_no_se_propone(
    client: AsyncClient, auth_headers: dict[str, Any], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    # El jean existe pero su stock inicial es de hoy: no respalda una venta del 22/10.
    await producto_con_compra(
        db_session, tid, nombre="Jeans azules T42", precio=Decimal("21000"), dia=today_ar()
    )
    await producto_con_compra(db_session, tid, nombre="buzo gris", precio=Decimal("20000"))
    await db_session.commit()
    rec = await _fila(db_session, sample_tenant)

    d = (await _sugerencias(client, auth_headers, rec)).json()

    assert d["solutions"] == []
    assert "coincide con «Jean»" in d["reason"]


async def test_una_fila_de_un_solo_producto_no_es_un_combo(
    client: AsyncClient, auth_headers: dict[str, Any], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    rec = await _fila(db_session, sample_tenant, detalle="Buzo canguro")
    d = (await _sugerencias(client, auth_headers, rec)).json()
    assert d["solutions"] == [] and d["references"] == []


async def test_registra_una_venta_por_linea_agrupadas_y_con_la_procedencia(
    client: AsyncClient, auth_headers: dict[str, Any], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    jean, _j40, buzo = await _jean_y_buzo(db_session, sample_tenant)
    rec = await _fila(db_session, sample_tenant)

    r = await _reclasificar(
        client, auth_headers, rec, _items((jean, 1, "21000.00"), (buzo, 1, "20000.00"))
    )

    assert r.status_code == 200, r.text
    ventas = await _ventas(db_session, sample_tenant)
    assert sorted(v.amount for v in ventas) == [Decimal("20000.00"), Decimal("21000.00")]
    assert sum(v.amount for v in ventas) == Decimal("41000.00")
    grupos = {(v.custom_fields or {}).get("sale_group_id") for v in ventas}
    assert len(grupos) == 1 and None not in grupos
    assert {v.product_id for v in ventas} == {jean.id, buzo.id}
    assert {v.provenance for v in ventas} == {"REAL"}
    await db_session.refresh(rec)
    assert rec.status == "IMPORTED"
    await db_session.refresh(jean)
    await db_session.refresh(buzo)
    assert (jean.stock_units, buzo.stock_units) == (99, 99)


async def test_la_suma_que_no_cierra_no_escribe_nada(
    client: AsyncClient, auth_headers: dict[str, Any], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    jean, _j40, buzo = await _jean_y_buzo(db_session, sample_tenant)
    rec = await _fila(db_session, sample_tenant)

    r = await _reclasificar(
        client, auth_headers, rec, _items((jean, 1, "21000.00"), (buzo, 1, "19999.00"))
    )

    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "COMBO_SUMA_NO_COINCIDE"
    assert await _ventas(db_session, sample_tenant) == []
    await db_session.refresh(rec)
    assert rec.status == "PENDING"


async def test_producto_repetido_es_400(
    client: AsyncClient, auth_headers: dict[str, Any], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    jean, _j40, _buzo = await _jean_y_buzo(db_session, sample_tenant)
    rec = await _fila(db_session, sample_tenant, monto="42000")

    r = await _reclasificar(
        client, auth_headers, rec, _items((jean, 1, "21000.00"), (jean, 1, "21000.00")),
        monto=42000,
    )

    assert r.status_code == 400, r.text
    assert await _ventas(db_session, sample_tenant) == []


async def test_una_linea_sin_respaldo_revierte_todo(
    client: AsyncClient, auth_headers: dict[str, Any], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    jean = await producto_con_compra(db_session, tid, nombre="Jean ok", precio=Decimal("21000"))
    nuevo = await producto_con_compra(
        db_session, tid, nombre="Buzo nuevo", precio=Decimal("20000"), dia=date(2026, 11, 5)
    )
    await db_session.commit()
    rec = await _fila(db_session, sample_tenant)

    r = await _reclasificar(
        client, auth_headers, rec, _items((jean, 1, "21000.00"), (nuevo, 1, "20000.00"))
    )

    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "SALE_WITHOUT_PURCHASE"
    assert r.json()["detail"]["product_name"] == "Buzo nuevo"
    assert await _ventas(db_session, sample_tenant) == []
    await db_session.refresh(jean)
    assert jean.stock_units == 100


async def test_stock_insuficiente_en_una_linea_revierte_todo(
    client: AsyncClient, auth_headers: dict[str, Any], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    jean = await producto_con_compra(db_session, tid, nombre="Jean ok", precio=Decimal("21000"))
    buzo = await producto_con_compra(
        db_session, tid, nombre="Buzo escaso", precio=Decimal("20000"), stock=1
    )
    await db_session.commit()
    # Pide 2 buzos y hay 1: la segunda línea no tiene stock y la primera no se guarda.
    rec = await _fila(db_session, sample_tenant, monto="61000")

    r = await _reclasificar(
        client, auth_headers, rec, _items((jean, 1, "21000.00"), (buzo, 2, "20000.00")),
        monto=61000,
    )

    assert r.status_code in (400, 422), r.text
    assert await _ventas(db_session, sample_tenant) == []
    await db_session.refresh(jean)
    assert jean.stock_units == 100


async def test_fiado_sin_cliente_real_es_400_y_no_escribe(
    client: AsyncClient, auth_headers: dict[str, Any], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    jean, _j40, buzo = await _jean_y_buzo(db_session, sample_tenant)
    rec = await _fila(db_session, sample_tenant, medio="account")

    r = await _reclasificar(
        client, auth_headers, rec, _items((jean, 1, "21000.00"), (buzo, 1, "20000.00")),
        medio="account",
    )

    assert r.status_code == 400, r.text
    assert await _ventas(db_session, sample_tenant) == []


async def test_los_movimientos_de_stock_llevan_el_archivo_de_la_fila(
    client: AsyncClient, auth_headers: dict[str, Any], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    from app.domain.ingestion_version import INGESTION_VERSION
    from app.persistence.models.file import PROCESSING_STATUS_DONE, UploadedFile

    jean, _j40, buzo = await _jean_y_buzo(db_session, sample_tenant)
    archivo = UploadedFile(
        tenant_id=sample_tenant.tenant_id,
        uploaded_by=None,
        original_filename="ventas.xlsx",
        s3_key=f"uploads/test/{uuid.uuid4()}/ventas.xlsx",
        content_type="text/csv",
        size_bytes=10,
        purpose="ventas",
        status="uploaded",
        processing_status=PROCESSING_STATUS_DONE,
        parsed_summary_json={},
        ingestion_version=INGESTION_VERSION,
    )
    db_session.add(archivo)
    await db_session.flush()
    rec = await _fila(db_session, sample_tenant)
    rec.uploaded_file_id = archivo.id
    await db_session.commit()

    r = await _reclasificar(
        client, auth_headers, rec, _items((jean, 1, "21000.00"), (buzo, 1, "20000.00"))
    )

    assert r.status_code == 200, r.text
    ventas = await _ventas(db_session, sample_tenant)
    assert {v.source_upload_id for v in ventas} == {archivo.id}
    refs = {v.source_row_ref for v in ventas}
    assert len(refs) == 1 and next(iter(refs)).endswith(str(rec.id))
    movs = (
        await db_session.execute(
            select(InventoryMovement).where(
                InventoryMovement.tenant_id == sample_tenant.tenant_id,
                InventoryMovement.movement_type == "sale",
            )
        )
    ).scalars().all()
    assert len(movs) == 2 and {m.source_upload_id for m in movs} == {archivo.id}
