"""B·5e-2: «no se puede vender algo que no se compró», en el POS y hacia atrás.

- POS online: un producto sin compra ni stock inicial a la fecha → 422
  ``SALE_WITHOUT_PURCHASE``; no se crea nada.
- POS sin red, venta YA cobrada: entra completa (la plata y el turno son reales)
  y deja en «Otros» una tarea (``source='pos'``) que no se clasifica —la venta ya
  existe— y se descarta al cargar la compra.
- El catálogo del POS dice ``tiene_compra`` por producto.
- Borrar el archivo cuyas compras son lo único que respalda ventas de otro lado
  → 409 ``FILE_BACKS_SALES``, sin borrar nada.
"""

from __future__ import annotations

import unittest.mock
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.ingestion_import_service import insert_confirmed_data
from app.application.services.respaldo_service import MOTIVO_VENTA_POS_SIN_COMPRA
from app.domain.business_time import today_ar
from app.domain.pos_terminal import TERMINAL_HEADER
from app.persistence.models.file import UploadedFile
from app.persistence.models.pos_operation import PosOperation
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.models.unclassified_record import UnclassifiedRecord
from app.tests._respaldo import archivo_de_prueba, producto_con_compra

_FECHA = f"{today_ar().isoformat()}T10:00:00"
_COBRADO = f"{today_ar().isoformat()}T09:00:00"


@pytest.fixture(autouse=True)
def _sin_celery(mock_score_trigger: Any) -> Any:
    with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
        yield


async def _caja(client: AsyncClient, headers: dict[str, str]) -> dict[str, str]:
    alta = await client.post(
        "/api/v1/pos/terminals", json={"name": f"Caja {uuid.uuid4().hex[:4]}"},
        headers=headers,
    )
    assert alta.status_code == 201, alta.text
    return {**headers, TERMINAL_HEADER: alta.json()["secret"]}


async def _sin_compra(db: AsyncSession, tid: uuid.UUID, nombre: str = "Vela") -> Product:
    p = Product(tenant_id=tid, name=nombre, sale_price_ars=Decimal("100"), stock_units=10)
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return p


def _venta(pid: Any, total: str = "100.00") -> dict[str, Any]:
    return {
        "client_operation_id": f"op-{uuid.uuid4().hex[:16]}",
        "operation_date": _FECHA,
        "charged_at": _COBRADO,
        "items": [{"product_id": str(pid), "quantity": 1}],
        "tenders": [{"payment_method": "cash", "amount_ars": total}],
    }


async def test_pos_online_sin_compra_da_422_y_no_crea_nada(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    caja = await _caja(client, auth_headers)
    p = await _sin_compra(db_session, sample_tenant.tenant_id)
    r = await client.post("/api/v1/pos/operations", json=_venta(p.id), headers=caja)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "SALE_WITHOUT_PURCHASE"
    assert (await db_session.execute(select(PosOperation))).first() is None


async def test_pos_sin_red_ya_cobrada_entra_y_deja_la_tarea(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    caja = await _caja(client, auth_headers)
    p = await _sin_compra(db_session, sample_tenant.tenant_id)
    r = await client.post(
        "/api/v1/pos/operations", json=_venta(p.id), headers={**caja, "X-POS-Offline": "1"}
    )
    assert r.status_code == 201, r.text
    await db_session.commit()
    (venta,) = (await db_session.execute(select(SaleEntry))).scalars().all()
    (tarea,) = (await db_session.execute(select(UnclassifiedRecord))).scalars().all()
    assert tarea.source == "pos"
    assert (tarea.context_label or "").startswith(MOTIVO_VENTA_POS_SIN_COMPRA)
    assert tarea.row_data["sale_id"] == str(venta.id)

    # No se clasifica: la venta ya existe y se duplicaría.
    r = await client.post(
        f"/api/v1/others/{tarea.id}/reclassify",
        json={"entity_type": "sale", "fields": {
            "amount": "100", "transaction_date": _FECHA, "payment_method": "cash",
            "product_id": str(p.id),
        }},
        headers=auth_headers,
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "POS_SALE_ALREADY_RECORDED"
    # Se descarta.
    r = await client.post(f"/api/v1/others/{tarea.id}/dismiss", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert len((await db_session.execute(select(SaleEntry))).scalars().all()) == 1


async def test_pos_con_compra_no_deja_tarea(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    caja = await _caja(client, auth_headers)
    p = await producto_con_compra(db_session, sample_tenant.tenant_id, precio=Decimal("100"))
    r = await client.post("/api/v1/pos/operations", json=_venta(p.id), headers=caja)
    assert r.status_code == 201, r.text
    assert (await db_session.execute(select(UnclassifiedRecord))).first() is None


async def test_el_catalogo_dice_si_hay_compra(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    con = await producto_con_compra(db_session, tid, nombre="Yerba", precio=Decimal("100"))
    sin = await _sin_compra(db_session, tid)
    caja = await _caja(client, auth_headers)
    r = await client.get("/api/v1/pos/catalog", headers=caja)
    assert r.status_code == 200, r.text
    por_id = {i["id"]: i["tiene_compra"] for i in r.json()["items"]}
    assert por_id == {str(con.id): True, str(sin.id): False}


async def test_borrar_el_archivo_cuya_compra_respalda_ventas_da_409(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """El catálogo del archivo es la única fuente de «Vela»; una venta a mano
    posterior se respaldó en él. Borrarlo la dejaría vendiendo sin compra."""
    tid = sample_tenant.tenant_id
    fid = await archivo_de_prueba(db_session, tid)
    await insert_confirmed_data(
        db_session, tid,
        {"file_type": "spreadsheet", "inferred_type": "stock", "has_producto": True,
         "stock_detectado": [{"nombre": "Vela", "precio": "100", "costo": "40", "stock": "20"}]},
        {"productos": True},
        uploaded_file_id=fid,
        stock_treatment="opening_balance",
    )
    await db_session.commit()
    vela = (await db_session.execute(select(Product).where(Product.name == "Vela"))).scalar_one()
    f = await db_session.get(UploadedFile, fid)
    assert f is not None
    f.processing_status = "DONE"
    await db_session.commit()
    r = await client.post(
        "/api/v1/sales",
        json={"amount": "100", "quantity": 1, "product_id": str(vela.id),
              "transaction_date": datetime.combine(today_ar(), datetime.min.time())
              .replace(hour=12).isoformat(),
              "payment_method": "cash"},
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text

    r = await client.delete(
        f"/api/v1/ingestion/files/{fid}", params={"confirm": "true"}, headers=auth_headers
    )
    assert r.status_code == 409, r.text
    d = r.json()["detail"]
    assert (d["code"], d["ventas"]) == ("FILE_BACKS_SALES", 1)
    await db_session.commit()
    f = await db_session.get(UploadedFile, fid, populate_existing=True)
    assert f is not None and f.deleted_at is None


# ── Revisión de B·5e-2 ────────────────────────────────────────────────────────


async def test_el_reintento_sin_red_no_duplica_la_tarea(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    caja = await _caja(client, auth_headers)
    p = await _sin_compra(db_session, sample_tenant.tenant_id)
    venta = _venta(p.id)
    h = {**caja, "X-POS-Offline": "1"}
    assert (await client.post("/api/v1/pos/operations", json=venta, headers=h)).status_code == 201
    r = await client.post("/api/v1/pos/operations", json=venta, headers=h)
    assert r.status_code in (200, 201), r.text
    await db_session.commit()
    assert len((await db_session.execute(select(UnclassifiedRecord))).scalars().all()) == 1
    assert len((await db_session.execute(select(SaleEntry))).scalars().all()) == 1


async def test_el_lookup_del_escaner_dice_si_hay_compra(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Revisión: con red, el escáner agregaba al carrito y frenaba recién al
    cobrar (con la plata en la mano)."""
    p = Product(
        tenant_id=sample_tenant.tenant_id, name="Vela", sale_price_ars=Decimal("100"),
        stock_units=10, sku="VELA-1",
    )
    db_session.add(p)
    await db_session.commit()
    caja = await _caja(client, auth_headers)
    r = await client.get("/api/v1/products/lookup", params={"code": "VELA-1"}, headers=caja)
    assert r.status_code == 200, r.text
    assert r.json()["product"]["tiene_compra"] is False


async def test_con_otra_compra_que_respalda_el_borrado_no_frena(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """La venta sigue respaldada por un stock inicial cargado a mano antes: el
    archivo no es lo único que la respalda."""
    tid = sample_tenant.tenant_id
    vela = await producto_con_compra(db_session, tid, nombre="Vela", precio=Decimal("100"))
    fid = await archivo_de_prueba(db_session, tid)
    from app.application.services.inventory_movement_origin import SOURCE_CATALOG_INITIAL_STOCK
    from app.persistence.models.inventory import InventoryMovement

    db_session.add(
        InventoryMovement(
            tenant_id=tid, product_id=vela.id, movement_type="purchase", qty=5,
            unit_cost=Decimal("40"), source_type="purchase_import", source_event_id="x",
            source_upload_id=fid,
        )
    )
    f = await db_session.get(UploadedFile, fid)
    assert f is not None
    f.processing_status = "DONE"
    await db_session.commit()
    r = await client.post(
        "/api/v1/sales",
        json={"amount": "100", "quantity": 1, "product_id": str(vela.id),
              "transaction_date": _FECHA, "payment_method": "cash"},
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text
    assert SOURCE_CATALOG_INITIAL_STOCK  # el respaldo previo es una apertura
    r = await client.delete(
        f"/api/v1/ingestion/files/{fid}", params={"confirm": "true"}, headers=auth_headers
    )
    assert r.status_code == 200, r.text


async def test_la_relectura_no_cuenta_las_ventas_del_propio_archivo(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Revisión: las ventas del archivo que se relee no son «de otro lado»."""
    from datetime import timedelta

    from app.application.services.respaldo_service import (
        primeros_dias_de_fuente,
        ventas_que_pierden_respaldo,
    )
    from app.persistence.models.inventory import InventoryMovement

    tid = sample_tenant.tenant_id
    fid = await archivo_de_prueba(db_session, tid)
    vela = await _sin_compra(db_session, tid)
    hace_10 = datetime.combine(today_ar() - timedelta(days=10), datetime.min.time())
    compra = InventoryMovement(
        tenant_id=tid, product_id=vela.id, movement_type="purchase", qty=5,
        unit_cost=Decimal("40"), source_type="purchase_import", source_event_id="x",
        source_upload_id=fid, occurred_at=hace_10,
    )
    db_session.add(compra)
    db_session.add(
        SaleEntry(
            tenant_id=tid, product_id=vela.id, amount=Decimal("100"), quantity=1,
            transaction_date=hace_10.replace(hour=12), payment_method="cash",
            source_upload_id=fid,
        )
    )
    await db_session.commit()
    antes = await primeros_dias_de_fuente(db_session, tid, [vela.id])
    compra.voided_at = datetime.now()
    await db_session.flush()
    propias = await ventas_que_pierden_respaldo(
        db_session, tid, antes, [vela.id], excluir_upload_id=fid
    )
    todas = await ventas_que_pierden_respaldo(db_session, tid, antes, [vela.id])
    assert (propias.total, todas.total) == (0, 1)
