"""B·5c: el stock inicial se carga a mano, por producto o en tabla, desde el día uno.

El stock inicial es mercadería que compraron el dueño o los socios: va al COSTO
(obligatorio) y se vende a otro precio. Cuenta como compra previa: las ventas
``sin_costo`` del producto desde su fecha quedan costeadas con él; un costo ya
congelado no se revalúa.

- ``POST /products/{id}/opening``: 409 ``OPENING_EXISTS`` si ya hay una apertura
  viva (con los caminos que existen para corregirla), 422 sin costo o con fecha
  futura, 404 ajeno, 403 sin permiso, idempotente.
- ``POST /products/batch/preview`` + ``POST /products/batch``: coincidencias
  por SKU, barcode y nombre; elección obligatoria; token del preview; todo o nada.
- «Otros → producto nuevo» con stock exige costo.
"""

from __future__ import annotations

import unittest.mock
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import stock_service
from app.application.services.inventory_movement_origin import SOURCE_CATALOG_INITIAL_STOCK
from app.domain.business_time import today_ar
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.models.unclassified_record import (
    UNCLASSIFIED_STATUS_PENDING,
    UnclassifiedRecord,
)

HOY = today_ar()
HACE_30 = HOY - timedelta(days=30)
HACE_20 = HOY - timedelta(days=20)
HACE_10 = HOY - timedelta(days=10)


def _iso(d: Any, hora: int = 9) -> str:
    return datetime(d.year, d.month, d.day, hora).isoformat()


@pytest.fixture(autouse=True)
def _sin_celery(mock_score_trigger: Any) -> None:
    pass


async def _producto(db: AsyncSession, tenant_id: uuid.UUID, **kw: Any) -> Product:
    p = Product(
        tenant_id=tenant_id,
        name=str(kw.pop("name", "Vela")),
        sale_price_ars=Decimal("1000"),
        stock_units=0,
        **kw,
    )
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return p


async def _vender(
    db: AsyncSession, tenant_id: uuid.UUID, pid: uuid.UUID, dia: Any, qty: int = 3
) -> SaleEntry:
    v = SaleEntry(
        tenant_id=tenant_id, product_id=pid, amount=Decimal("1000") * qty, quantity=qty,
        transaction_date=datetime(dia.year, dia.month, dia.day, 12), payment_method="cash",
    )
    db.add(v)
    await db.commit()
    return v


async def _leer(db: AsyncSession, modelo: Any, id_: uuid.UUID) -> Any:
    # El fixture ``client`` comparte la sesión y no confirma: el commit dispara
    # los listeners (costeo, costo vigente) como en producción.
    await db.commit()
    return await db.get(modelo, id_, populate_existing=True)


async def _aperturas(db: AsyncSession, pid: uuid.UUID) -> list[InventoryMovement]:
    await db.commit()
    return list(
        (
            await db.execute(
                select(InventoryMovement)
                .where(
                    InventoryMovement.product_id == pid,
                    InventoryMovement.source_type == SOURCE_CATALOG_INITIAL_STOCK,
                    InventoryMovement.voided_at.is_(None),
                )
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )


def _apertura(qty: int = 10, costo: str = "400.00", dia: Any = HACE_30) -> dict[str, Any]:
    return {"stock_units": qty, "unit_cost_ars": costo, "fecha": _iso(dia)}


# ── POST /products/{id}/opening ───────────────────────────────────────────────


async def test_la_apertura_costea_las_ventas_sin_costo_desde_su_fecha(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Venta de 3 u × 1.000 sin costo; stock inicial a 400 → CMV 1.200."""
    tid = sample_tenant.tenant_id
    p = await _producto(db_session, tid)
    venta = await _vender(db_session, tid, p.id, HACE_10)
    assert (await _leer(db_session, SaleEntry, venta.id)).cost_origin == "sin_costo"

    r = await client.post(
        f"/api/v1/products/{p.id}/opening", json=_apertura(), headers=auth_headers
    )
    assert r.status_code == 201, r.text
    assert r.json()["ventas_sin_costo_desde_la_fecha"] == 1

    v = await _leer(db_session, SaleEntry, venta.id)
    assert (v.cost_origin, v.cost_ars) == ("stock_inicial", Decimal("1200.00"))
    (mov,) = await _aperturas(db_session, p.id)
    assert (mov.qty, mov.unit_cost, mov.opening_date_source) == (10, Decimal("400.00"), "usuario")
    assert mov.opening_confirmed_at is not None
    prod = await _leer(db_session, Product, p.id)
    assert prod.stock_units == 10
    audit = (
        await db_session.execute(
            select(DecisionAuditLog).where(
                DecisionAuditLog.tenant_id == tid,
                DecisionAuditLog.decision_type == "OPENING_STOCK_REGISTERED",
            )
        )
    ).scalar_one()
    assert audit.decision_data["movement_id"] == str(mov.id)


async def test_un_costo_ya_congelado_no_se_revalua(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Compra a 100 hace 20 días y una venta de 2 u costeada a 200. El stock
    inicial atrasado (hace 30, a 400) no cambia ese costo."""
    tid = sample_tenant.tenant_id
    p = await _producto(db_session, tid)
    with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
        await stock_service.increment_stock(
            p.id, tid, 10, Decimal("100"), None, db_session,
            occurred_at=datetime(HACE_20.year, HACE_20.month, HACE_20.day, 9),
        )
    await db_session.commit()
    venta = await _vender(db_session, tid, p.id, HACE_10, qty=2)
    antes = await _leer(db_session, SaleEntry, venta.id)
    assert (antes.cost_origin, antes.cost_ars) == ("compra", Decimal("200.00"))

    r = await client.post(
        f"/api/v1/products/{p.id}/opening", json=_apertura(), headers=auth_headers
    )
    assert r.status_code == 201, r.text
    assert r.json()["ventas_sin_costo_desde_la_fecha"] == 0
    despues = await _leer(db_session, SaleEntry, venta.id)
    assert (despues.cost_origin, despues.cost_ars) == ("compra", Decimal("200.00"))


async def test_con_apertura_viva_da_409_y_no_suma_otra(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    p = await _producto(db_session, sample_tenant.tenant_id)
    url = f"/api/v1/products/{p.id}/opening"
    assert (await client.post(url, json=_apertura(), headers=auth_headers)).status_code == 201
    r = await client.post(url, json=_apertura(qty=5), headers=auth_headers)
    assert r.status_code == 409, r.text
    d = r.json()["detail"]
    assert (d["code"], d["origen"], d["qty"], d["unit_cost"]) == (
        "OPENING_EXISTS", "manual", 10, "400.00"
    )
    assert d["fecha"] == HACE_30.isoformat()
    # Sólo caminos que existen: la fecha y la cantidad, nunca «corregir el costo».
    assert any("Confirmar stock inicial" in c for c in d["caminos"])
    assert not any("costo" in c.lower() for c in d["caminos"])
    assert len(await _aperturas(db_session, p.id)) == 1
    assert (await _leer(db_session, Product, p.id)).stock_units == 10


async def test_apertura_de_archivo_tambien_cuenta_y_apunta_al_archivo(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    from app.persistence.models.file import UploadedFile

    tid = sample_tenant.tenant_id
    f = UploadedFile(
        id=uuid.uuid4(), tenant_id=tid, uploaded_by=None, original_filename="catalogo.xlsx",
        s3_key="catalogo.xlsx", content_type="application/vnd.ms-excel", size_bytes=100,
        purpose="ingestion",
    )
    db_session.add(f)
    p = await _producto(db_session, tid)
    db_session.add(
        InventoryMovement(
            tenant_id=tid, product_id=p.id, movement_type="adjustment", qty=4,
            unit_cost=Decimal("50"), source_type=SOURCE_CATALOG_INITIAL_STOCK,
            source_event_id="import", source_upload_id=f.id,
        )
    )
    await db_session.commit()
    r = await client.post(
        f"/api/v1/products/{p.id}/opening", json=_apertura(), headers=auth_headers
    )
    assert r.status_code == 409, r.text
    d = r.json()["detail"]
    assert (d["origen"], d["archivo"]) == ("archivo", "catalogo.xlsx")
    assert "catalogo.xlsx" in d["caminos"][0]


@pytest.mark.parametrize(
    ("cuerpo", "campo"),
    [
        ({"stock_units": 10, "fecha": _iso(HACE_30)}, "unit_cost_ars"),
        ({"stock_units": 10, "unit_cost_ars": "0", "fecha": _iso(HACE_30)}, "unit_cost_ars"),
        ({"stock_units": 0, "unit_cost_ars": "400", "fecha": _iso(HACE_30)}, "stock_units"),
        ({"stock_units": 10, "unit_cost_ars": "400"}, "fecha"),
        (
            {"stock_units": 10, "unit_cost_ars": "400", "fecha": _iso(HOY + timedelta(days=2))},
            "fecha",
        ),
    ],
)
async def test_sin_costo_cantidad_o_fecha_valida_da_422(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    cuerpo: dict[str, Any],
    campo: str,
) -> None:
    p = await _producto(db_session, sample_tenant.tenant_id)
    r = await client.post(f"/api/v1/products/{p.id}/opening", json=cuerpo, headers=auth_headers)
    assert r.status_code == 422, r.text
    assert campo in r.text
    assert await _aperturas(db_session, p.id) == []


async def test_producto_ajeno_404_e_inactivo_409(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    second_tenant: Tenant,
) -> None:
    ajeno = await _producto(db_session, second_tenant.tenant_id)
    r = await client.post(
        f"/api/v1/products/{ajeno.id}/opening", json=_apertura(), headers=auth_headers
    )
    assert r.status_code == 404, r.text
    assert await _aperturas(db_session, ajeno.id) == []
    baja = await _producto(db_session, sample_tenant.tenant_id, is_active=False)
    r = await client.post(
        f"/api/v1/products/{baja.id}/opening", json=_apertura(), headers=auth_headers
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "PRODUCT_INACTIVE"


async def test_un_viewer_no_carga_stock_inicial(
    client: AsyncClient,
    viewer_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    p = await _producto(db_session, sample_tenant.tenant_id)
    r = await client.post(
        f"/api/v1/products/{p.id}/opening", json=_apertura(), headers=viewer_headers
    )
    assert r.status_code == 403, r.text
    assert await _aperturas(db_session, p.id) == []


async def test_el_reintento_con_la_misma_key_no_duplica(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    p = await _producto(db_session, sample_tenant.tenant_id)
    h = {**auth_headers, "Idempotency-Key": "op-1"}
    url = f"/api/v1/products/{p.id}/opening"
    assert (await client.post(url, json=_apertura(), headers=h)).status_code == 201
    r = await client.post(url, json=_apertura(), headers=h)
    assert r.json()["detail"]["code"] == "DUPLICATE_IDEMPOTENT"
    assert len(await _aperturas(db_session, p.id)) == 1


async def test_el_producto_dice_si_necesita_stock_inicial(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    p = await _producto(db_session, sample_tenant.tenant_id)
    url = f"/api/v1/products/{p.id}"
    assert (await client.get(url, headers=auth_headers)).json()["needs_initial_stock"] is True
    await client.post(f"{url}/opening", json=_apertura(), headers=auth_headers)
    await db_session.commit()
    assert (await client.get(url, headers=auth_headers)).json()["needs_initial_stock"] is False


# ── Lote en dos pasos ─────────────────────────────────────────────────────────


def _fila(nombre: str, **kw: Any) -> dict[str, Any]:
    return {
        "nombre": nombre,
        "sale_price_ars": "1000.00",
        "unit_cost_ars": "400.00",
        "stock_units": 10,
        **kw,
    }


async def _preview(
    client: AsyncClient, h: dict[str, str], filas: list[dict[str, Any]]
) -> dict[str, Any]:
    r = await client.post("/api/v1/products/batch/preview", json={"filas": filas}, headers=h)
    assert r.status_code == 200, r.text
    return dict(r.json())


async def _confirmar(
    client: AsyncClient,
    h: dict[str, str],
    filas: list[dict[str, Any]],
    token: str,
    elecciones: list[dict[str, Any]] | None = None,
) -> Any:
    return await client.post(
        "/api/v1/products/batch",
        json={
            "filas": filas,
            "elecciones": elecciones or [],
            "preview_token": token,
            "fecha": _iso(HACE_30),
        },
        headers=h,
    )


async def _cantidad_de_productos(db: AsyncSession, tid: uuid.UUID) -> int:
    await db.commit()
    return int(
        (
            await db.execute(select(func.count(Product.id)).where(Product.tenant_id == tid))
        ).scalar_one()
    )


async def test_lote_sin_coincidencias_crea_todo_con_su_stock_inicial(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    filas = [_fila("Yerba 1kg", sku="Y-1"), _fila("Azúcar", stock_units=4, unit_cost_ars="250")]
    pv = await _preview(client, auth_headers, filas)
    assert [f["coincidencias"] for f in pv["filas"]] == [[], []]
    r = await _confirmar(client, auth_headers, filas, pv["preview_token"])
    assert r.status_code == 201, r.text
    body = r.json()
    assert (body["creados"], body["con_apertura"]) == (2, 0)
    azucar = next(f for f in body["filas"] if f["fila"] == 1)
    (mov,) = await _aperturas(db_session, uuid.UUID(azucar["product_id"]))
    assert (mov.qty, mov.unit_cost) == (4, Decimal("250.00"))
    prod = await _leer(db_session, Product, uuid.UUID(azucar["product_id"]))
    assert (prod.name, prod.sale_price_ars, prod.stock_units) == ("Azúcar", Decimal("1000.00"), 4)


async def test_lote_con_coincidencia_exige_eleccion_y_abre_el_existente(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    existente = await _producto(db_session, tid, name="Coca-Cola 500")
    venta = await _vender(db_session, tid, existente.id, HACE_10)
    filas = [_fila("coca cola 500")]
    pv = await _preview(client, auth_headers, filas)
    (c,) = pv["filas"][0]["coincidencias"]
    assert (c["product_id"], c["por"], c["necesita_stock_inicial"]) == (
        str(existente.id), "nombre", True
    )

    r = await _confirmar(client, auth_headers, filas, pv["preview_token"])
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "CHOICE_REQUIRED"
    assert await _aperturas(db_session, existente.id) == []

    r = await _confirmar(
        client, auth_headers, filas, pv["preview_token"],
        [{"fila": 0, "accion": "apertura_en", "product_id": str(existente.id)}],
    )
    assert r.status_code == 201, r.text
    assert (r.json()["creados"], r.json()["con_apertura"]) == (0, 1)
    assert await _cantidad_de_productos(db_session, tid) == 1
    v = await _leer(db_session, SaleEntry, venta.id)
    assert (v.cost_origin, v.cost_ars) == ("stock_inicial", Decimal("1200.00"))


async def test_lote_crear_un_homonimo_si_se_elige(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    await _producto(db_session, tid, name="Vela")
    filas = [_fila("Vela")]
    pv = await _preview(client, auth_headers, filas)
    r = await _confirmar(
        client, auth_headers, filas, pv["preview_token"], [{"fila": 0, "accion": "crear"}]
    )
    assert r.status_code == 201, r.text
    assert await _cantidad_de_productos(db_session, tid) == 2


async def test_lote_no_crea_otro_con_el_sku_de_uno_existente(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    await _producto(db_session, sample_tenant.tenant_id, name="Fernet", sku="F-750")
    filas = [_fila("Fernet 750", sku="f-750")]
    pv = await _preview(client, auth_headers, filas)
    assert pv["filas"][0]["coincidencias"][0]["por"] == "sku"
    r = await _confirmar(
        client, auth_headers, filas, pv["preview_token"], [{"fila": 0, "accion": "crear"}]
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INVALID_CHOICE"


async def test_lote_no_abre_un_producto_que_ya_tiene_stock_inicial(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    p = await _producto(db_session, sample_tenant.tenant_id, name="Vela")
    await client.post(f"/api/v1/products/{p.id}/opening", json=_apertura(), headers=auth_headers)
    await db_session.commit()
    filas = [_fila("Vela")]
    pv = await _preview(client, auth_headers, filas)
    assert pv["filas"][0]["coincidencias"][0]["necesita_stock_inicial"] is False
    r = await _confirmar(
        client, auth_headers, filas, pv["preview_token"],
        [{"fila": 0, "accion": "apertura_en", "product_id": str(p.id)}],
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INVALID_CHOICE"
    assert len(await _aperturas(db_session, p.id)) == 1


async def test_token_viejo_da_409_con_el_preview_nuevo(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Entre el preview y la confirmación aparece un homónimo: lo que el usuario
    eligió ya no es lo que hay."""
    tid = sample_tenant.tenant_id
    filas = [_fila("Vela")]
    pv = await _preview(client, auth_headers, filas)
    await _producto(db_session, tid, name="VELA")
    r = await _confirmar(client, auth_headers, filas, pv["preview_token"])
    assert r.status_code == 409, r.text
    d = r.json()["detail"]
    assert d["code"] == "BATCH_CHANGED"
    assert d["preview"]["filas"][0]["coincidencias"][0]["nombre"] == "VELA"
    assert d["preview"]["preview_token"] != pv["preview_token"]
    assert await _cantidad_de_productos(db_session, tid) == 1


async def test_lote_todo_o_nada(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """La fila 3 repite el nombre de la 1: no se crea ninguna."""
    tid = sample_tenant.tenant_id
    filas = [_fila("Yerba"), _fila("Azúcar"), _fila("yerba")]
    pv = await _preview(client, auth_headers, filas)
    assert pv["filas"][2]["errores"] == ["Repite el nombre de la fila 1."]
    r = await _confirmar(client, auth_headers, filas, pv["preview_token"])
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "ROWS_INVALID"
    assert await _cantidad_de_productos(db_session, tid) == 0


async def test_lote_falla_a_mitad_y_no_deja_nada(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """La segunda fila falla en medio del alta: lo que la primera ya escribió se
    deshace con ella (nada confirma a mitad de camino)."""
    from app.api.v1 import _stock_manual
    from app.api.v1 import products as rutas

    tid = sample_tenant.tenant_id
    filas = [_fila("Yerba"), _fila("Azúcar", stock_units=3)]
    pv = await _preview(client, auth_headers, filas)
    real = _stock_manual.registrar_apertura_http
    llamadas = 0

    async def _falla_la_segunda(*a: Any, **k: Any) -> Any:
        nonlocal llamadas
        llamadas += 1
        if llamadas == 2:
            raise RuntimeError("boom")
        return await real(*a, **k)

    with unittest.mock.patch.object(rutas, "registrar_apertura_http", _falla_la_segunda):
        r = await _confirmar(client, auth_headers, filas, pv["preview_token"])
    assert r.status_code == 500
    await db_session.rollback()
    assert llamadas == 2
    assert await _cantidad_de_productos(db_session, tid) == 0


async def test_lote_viewer_403(
    client: AsyncClient, viewer_headers: dict[str, str]
) -> None:
    r = await client.post(
        "/api/v1/products/batch/preview", json={"filas": [_fila("Vela")]}, headers=viewer_headers
    )
    assert r.status_code == 403


async def test_el_reintento_del_lote_con_la_misma_key_no_duplica(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    filas = [_fila("Yerba")]
    pv = await _preview(client, auth_headers, filas)
    h = {**auth_headers, "Idempotency-Key": "lote-1"}
    assert (await _confirmar(client, h, filas, pv["preview_token"])).status_code == 201
    await db_session.commit()
    r = await _confirmar(client, h, filas, pv["preview_token"])
    assert r.json()["detail"]["code"] == "DUPLICATE_IDEMPOTENT"
    assert await _cantidad_de_productos(db_session, sample_tenant.tenant_id) == 1


# ── «Otros → producto nuevo» con stock ────────────────────────────────────────


async def test_otros_producto_nuevo_con_stock_exige_costo(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    rec = UnclassifiedRecord(
        tenant_id=tid, source="ingestion", context_label="x",
        row_data={"detalle": "Vela"}, suggested_entity="product",
        status=UNCLASSIFIED_STATUS_PENDING,
    )
    db_session.add(rec)
    await db_session.commit()
    r = await client.post(
        f"/api/v1/others/{rec.id}/reclassify",
        json={
            "entity_type": "product",
            "fields": {
                "name": "Vela", "sale_price_ars": "1000", "stock_units": 5,
                "acquired_at": _iso(HACE_30),
            },
        },
        headers=auth_headers,
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "COSTO_REQUERIDO_CON_STOCK"
    assert await _cantidad_de_productos(db_session, tid) == 0


# ── Revisión de B·5c ──────────────────────────────────────────────────────────


async def test_dos_filas_sobre_el_mismo_producto_dan_422(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Una coincide por SKU y otra por nombre: no es «ya tiene stock inicial»
    (un 409 con un movimiento revertido), es una elección inválida."""
    p = await _producto(db_session, sample_tenant.tenant_id, name="Vela", sku="V-1")
    filas = [_fila("Vela grande", sku="V-1"), _fila("Vela")]
    pv = await _preview(client, auth_headers, filas)
    r = await _confirmar(
        client, auth_headers, filas, pv["preview_token"],
        [
            {"fila": 0, "accion": "apertura_en", "product_id": str(p.id)},
            {"fila": 1, "accion": "apertura_en", "product_id": str(p.id)},
        ],
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "INVALID_CHOICE"
    assert await _aperturas(db_session, p.id) == []


async def test_el_token_no_cambia_por_como_se_escribe_el_precio(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    pv = await _preview(
        client, auth_headers, [_fila("Yerba", sale_price_ars="1000", unit_cost_ars="400")]
    )
    r = await _confirmar(
        client, auth_headers,
        [_fila("Yerba", sale_price_ars="1000.00", unit_cost_ars="400.0")],
        pv["preview_token"],
    )
    assert r.status_code == 201, r.text


async def test_apertura_en_dice_lo_que_no_aplico(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """La fila trae otro SKU y otro precio: el stock inicial se carga, el
    producto no se edita, y la respuesta lo dice."""
    p = await _producto(db_session, sample_tenant.tenant_id, name="Vela")
    filas = [_fila("Vela", sku="NUEVO-1", sale_price_ars="2500")]
    pv = await _preview(client, auth_headers, filas)
    r = await _confirmar(
        client, auth_headers, filas, pv["preview_token"],
        [{"fila": 0, "accion": "apertura_en", "product_id": str(p.id)}],
    )
    assert r.status_code == 201, r.text
    assert r.json()["filas"][0]["campos_no_aplicados"] == ["sku", "sale_price_ars"]
    prod = await _leer(db_session, Product, p.id)
    assert (prod.sku, prod.sale_price_ars, prod.stock_units) == (None, Decimal("1000.00"), 10)


async def test_apertura_en_un_producto_por_peso_lee_el_stock_en_kg(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """B·5d: la fila dice 3 (kg) sobre un producto que se vende por kilo: el
    ledger cuenta gramos → 3.000, no 3 gramos."""
    p = await _producto(
        db_session, sample_tenant.tenant_id, name="Queso cremoso",
        sale_unit="gram", base_units_per_sale_unit=1000,
    )
    filas = [_fila("Queso cremoso", stock_units=3)]
    pv = await _preview(client, auth_headers, filas)
    assert pv["filas"][0]["coincidencias"][0]["sale_unit"] == "gram"
    r = await _confirmar(
        client, auth_headers, filas, pv["preview_token"],
        [{"fila": 0, "accion": "apertura_en", "product_id": str(p.id)}],
    )
    assert r.status_code == 201, r.text
    (mov,) = await _aperturas(db_session, p.id)
    assert mov.qty == 3000
    assert (await _leer(db_session, Product, p.id)).stock_units == 3000
