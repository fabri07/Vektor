"""M·2d-1b: el import deja el stock inicial sin confirmar y pide la fecha después.

- El confirm de un catálogo «ya la tenía» devuelve ``aperturas_por_confirmar``
  con lo que de verdad cargó: productos, unidades de venta, cuántos traen fecha
  en su fila y el momento cero como SUGERENCIA (no fecha nada).
- ``.../opening-confirmation`` con ``aceptar_fechas_de_filas`` confirma con su
  fecha las que la traen en la fila y le da ``fecha`` al resto; sin ``fecha`` y
  con alguna sin fecha propia → 422. Sin aceptarlas, ``fecha`` pisa todas.
- Una baja de catálogo (ajuste negativo) no es stock inicial: ni aviso ni fecha.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.apertura_service import resumen_de_archivo
from app.application.services.facts_provider import aperturas_por_confirmar
from app.application.services.inventory_movement_origin import (
    FECHA_DE_ARCHIVO,
    FECHA_DE_USUARIO,
    SOURCE_CATALOG_INITIAL_STOCK,
)
from app.domain.business_time import today_ar
from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry

HOY = today_ar()
HACE_90 = HOY - timedelta(days=90)
HACE_60 = HOY - timedelta(days=60)
HACE_30 = HOY - timedelta(days=30)
_CTX = "sheet:stock"


@pytest.fixture(autouse=True)
def _sin_celery(mock_score_trigger: Any) -> None:
    pass


def _iso(d: Any, hora: int = 9) -> str:
    return datetime(d.year, d.month, d.day, hora).isoformat()


def _a_medianoche(d: Any) -> datetime:
    return datetime.combine(d, datetime.min.time(), UTC)


async def _archivo(
    db: AsyncSession, tenant_id: uuid.UUID, filas: list[dict[str, Any]]
) -> UploadedFile:
    filas = [dict(f, __context__=_CTX) for f in filas]
    headers = sorted({k for f in filas for k in f if k != "__context__"})
    record = UploadedFile(
        tenant_id=tenant_id,
        uploaded_by=None,
        original_filename="stock.xlsx",
        s3_key="uploads/test/stock.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=2048,
        purpose="stock",
        status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        parsed_summary_json={
            "confidence": "HIGH",
            "file_type": "spreadsheet",
            "inferred_type": "mixed",
            "multi_sheet": True,
            "has_producto": True,
            "row_count": len(filas),
            "stock_detectado": filas,
            "mapping_contexts": [
                {
                    "context_id": _CTX,
                    "label": "stock",
                    "source_kind": "sheet",
                    "entity_type": "product",
                    "headers": headers,
                    "fields": None,
                    "preview_rows": filas,
                    "row_count": len(filas),
                }
            ],
        },
    )
    db.add(record)
    await db.commit()
    return record


def _map(source: str, target: str) -> dict[str, Any]:
    return {
        "source_column": source,
        "target_field": target,
        "context_id": _CTX,
        "entity_type": "product",
    }


async def _confirmar_import(
    client: AsyncClient, headers: dict[str, str], archivo: UploadedFile, *, con_fecha: bool
) -> dict[str, Any]:
    mapeo = [
        _map("Producto", "name"),
        _map("Stock", "stock_units"),
        _map("Costo", "unit_cost_ars"),
        _map("Precio", "sale_price_ars"),
    ]
    if con_fecha:
        mapeo.append(_map("Desde", "acquired_at"))
    resp = await client.post(
        f"/api/v1/ingestion/files/{archivo.id}/confirm",
        json={
            "column_mappings": mapeo,
            "confirmed_fields": {"productos": True},
            "context_confirmed": {_CTX: True},
            "stock_treatment": {_CTX: "opening_balance"},
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def _aperturas_del_archivo(db: AsyncSession, upload_id: uuid.UUID) -> list[InventoryMovement]:
    await db.commit()
    return list(
        (
            await db.execute(
                select(InventoryMovement)
                .where(
                    InventoryMovement.source_upload_id == upload_id,
                    InventoryMovement.source_type == SOURCE_CATALOG_INITIAL_STOCK,
                )
                .order_by(InventoryMovement.qty)
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )


def _fila(nombre: str, stock: int, desde: Any | None = None) -> dict[str, Any]:
    fila: dict[str, Any] = {"Producto": nombre, "Stock": str(stock), "Costo": "60", "Precio": "100"}
    if desde is not None:
        fila["Desde"] = desde.isoformat()
    return fila


async def _venta_previa(db: AsyncSession, tenant_id: uuid.UUID, dia: Any) -> None:
    db.add(
        SaleEntry(
            tenant_id=tenant_id, amount=Decimal("100"),
            transaction_date=datetime(dia.year, dia.month, dia.day, 12),
        )
    )
    await db.commit()


# ── El confirm del import ────────────────────────────────────────────────────


async def test_el_confirm_pide_la_fecha_con_lo_cargado_y_sugiere_el_momento_cero(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    await _venta_previa(db_session, tid, HACE_90)
    archivo = await _archivo(
        db_session, tid, [_fila("Vela", 10, HACE_60), _fila("Jabón", 7), _fila("Sahumerio", 3)]
    )
    body = await _confirmar_import(client, auth_headers, archivo, con_fecha=True)

    bloque = body["aperturas_por_confirmar"]
    assert bloque is not None
    assert bloque["aperturas"] == 3
    assert bloque["productos"] == 3
    assert Decimal(bloque["unidades"]) == Decimal("20")
    assert bloque["pendientes"] == 2
    assert bloque["con_fecha_de_fila"] == 1
    assert bloque["fecha_de_fila_min"] == HACE_60.isoformat()
    # La sugerencia es el primer día registrado, pero NO fechó nada.
    assert bloque["fecha_sugerida"] == HACE_90.isoformat()

    movs = {m.qty: m for m in await _aperturas_del_archivo(db_session, archivo.id)}
    assert all(m.opening_confirmed_at is None for m in movs.values())
    assert movs[10].opening_date_source == FECHA_DE_ARCHIVO
    assert movs[10].occurred_at is not None and movs[10].occurred_at.date() == HACE_60
    for qty in (7, 3):
        pendiente = movs[qty]
        assert pendiente.opening_date_source is None
        assert pendiente.occurred_at is not None and pendiente.occurred_at.date() == HOY


async def test_un_catalogo_comprado_no_pide_fecha(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    archivo = await _archivo(db_session, sample_tenant.tenant_id, [_fila("Vela", 10)])
    resp = await client.post(
        f"/api/v1/ingestion/files/{archivo.id}/confirm",
        json={
            "column_mappings": [
                _map("Producto", "name"), _map("Stock", "stock_units"),
                _map("Costo", "unit_cost_ars"), _map("Precio", "sale_price_ars"),
            ],
            "confirmed_fields": {"productos": True},
            "context_confirmed": {_CTX: True},
            "stock_treatment": {_CTX: "purchase"},
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["aperturas_por_confirmar"] is None


async def test_las_unidades_van_en_unidades_de_venta(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    upload_id = uuid.uuid4()
    p = Product(
        tenant_id=tid, name="Queso", sale_price_ars=Decimal("100"), stock_units=2500,
        sale_unit="gram", base_units_per_sale_unit=1000,
    )
    db_session.add(p)
    await db_session.flush()
    db_session.add(
        InventoryMovement(
            tenant_id=tid, product_id=p.id, movement_type="adjustment", qty=2500,
            unit_cost=Decimal("5"), source_type=SOURCE_CATALOG_INITIAL_STOCK,
            source_event_id="import", source_upload_id=upload_id,
            occurred_at=_a_medianoche(HOY),
        )
    )
    await db_session.flush()
    resumen = await resumen_de_archivo(db_session, tid, upload_id)
    assert resumen is not None
    assert resumen.unidades == Decimal("2.5")


# ── Confirmar por archivo, con la fecha de las filas ─────────────────────────


async def _importado(
    client: AsyncClient, headers: dict[str, str], db: AsyncSession, tenant_id: uuid.UUID,
    filas: list[dict[str, Any]],
) -> UploadedFile:
    archivo = await _archivo(db, tenant_id, filas)
    await _confirmar_import(client, headers, archivo, con_fecha=True)
    await db.commit()
    return archivo


async def test_aceptar_las_fechas_de_fila_sin_fecha_confirma_si_todas_la_traen(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    archivo = await _importado(
        client, auth_headers, db_session, sample_tenant.tenant_id,
        [_fila("Vela", 10, HACE_60), _fila("Jabón", 7, HACE_30)],
    )
    resp = await client.post(
        f"/api/v1/ingestion/files/{archivo.id}/opening-confirmation",
        json={"aceptar_fechas_de_filas": True},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    movs = {m.qty: m for m in await _aperturas_del_archivo(db_session, archivo.id)}
    assert movs[10].occurred_at is not None and movs[10].occurred_at.date() == HACE_60
    assert movs[7].occurred_at is not None and movs[7].occurred_at.date() == HACE_30
    for m in movs.values():
        assert m.opening_confirmed_at is not None
        assert m.opening_date_source == FECHA_DE_ARCHIVO


async def test_mixto_sin_fecha_es_422_y_no_confirma_nada(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    archivo = await _importado(
        client, auth_headers, db_session, sample_tenant.tenant_id,
        [_fila("Vela", 10, HACE_60), _fila("Jabón", 7)],
    )
    resp = await client.post(
        f"/api/v1/ingestion/files/{archivo.id}/opening-confirmation",
        json={"aceptar_fechas_de_filas": True},
        headers=auth_headers,
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["code"] == "FECHA_STOCK_INICIAL_REQUERIDA"
    upload_id = archivo.id  # el rollback expira el objeto
    await db_session.rollback()
    assert all(
        m.opening_confirmed_at is None
        for m in await _aperturas_del_archivo(db_session, upload_id)
    )


async def test_mixto_con_fecha_le_da_la_fecha_al_resto_y_conserva_la_de_la_fila(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    archivo = await _importado(
        client, auth_headers, db_session, sample_tenant.tenant_id,
        [_fila("Vela", 10, HACE_60), _fila("Jabón", 7)],
    )
    resp = await client.post(
        f"/api/v1/ingestion/files/{archivo.id}/opening-confirmation",
        json={"fecha": _iso(HACE_90), "aceptar_fechas_de_filas": True},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    movs = {m.qty: m for m in await _aperturas_del_archivo(db_session, archivo.id)}
    assert movs[10].occurred_at is not None and movs[10].occurred_at.date() == HACE_60
    assert movs[10].opening_date_source == FECHA_DE_ARCHIVO
    assert movs[7].occurred_at is not None and movs[7].occurred_at.date() == HACE_90
    assert movs[7].opening_date_source == FECHA_DE_USUARIO


async def test_sin_aceptar_la_fecha_del_usuario_pisa_la_de_la_fila(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    archivo = await _importado(
        client, auth_headers, db_session, sample_tenant.tenant_id,
        [_fila("Vela", 10, HACE_60)],
    )
    sin_fecha = await client.post(
        f"/api/v1/ingestion/files/{archivo.id}/opening-confirmation",
        json={},
        headers=auth_headers,
    )
    assert sin_fecha.status_code == 422, sin_fecha.text
    resp = await client.post(
        f"/api/v1/ingestion/files/{archivo.id}/opening-confirmation",
        json={"fecha": _iso(HACE_90)},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    (mov,) = await _aperturas_del_archivo(db_session, archivo.id)
    assert mov.occurred_at is not None and mov.occurred_at.date() == HACE_90
    assert mov.opening_date_source == FECHA_DE_USUARIO


async def test_adelantar_la_fecha_costea_las_ventas_del_mismo_archivo(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Sin fecha confirmada, las ventas anteriores a la carga quedan sin costo;
    al decir desde cuándo tenía el stock, M·2d-2 las costea."""
    tid = sample_tenant.tenant_id
    archivo = await _importado(client, auth_headers, db_session, tid, [_fila("Vela", 10)])
    (mov,) = await _aperturas_del_archivo(db_session, archivo.id)
    venta = SaleEntry(
        tenant_id=tid, product_id=mov.product_id, amount=Decimal("100"), quantity=1,
        transaction_date=datetime(HACE_30.year, HACE_30.month, HACE_30.day, 12),
        payment_method="cash",
    )
    db_session.add(venta)
    await db_session.commit()
    await db_session.refresh(venta)
    assert venta.cost_origin == "sin_costo"

    resp = await client.post(
        f"/api/v1/ingestion/files/{archivo.id}/opening-confirmation",
        json={"fecha": _iso(HACE_60)},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    await db_session.commit()
    recosteada = await db_session.get(SaleEntry, venta.id, populate_existing=True)
    assert recosteada is not None
    assert recosteada.cost_origin == "stock_inicial"


# ── El aviso y las bajas ─────────────────────────────────────────────────────


async def test_el_aviso_cuenta_las_fechas_de_fila(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    archivo = await _importado(
        client, auth_headers, db_session, sample_tenant.tenant_id,
        [_fila("Vela", 10, HACE_60), _fila("Jabón", 7)],
    )
    ap = await aperturas_por_confirmar(db_session, sample_tenant.tenant_id)
    (fila,) = ap.por_archivo
    assert fila.upload_id == archivo.id
    assert fila.aperturas == 2
    assert fila.con_fecha_de_fila == 1


async def test_una_baja_de_catalogo_no_es_stock_inicial(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Un catálogo con menos stock que el actual escribe un ajuste negativo: no
    entra al aviso ni se mueve al confirmar el archivo."""
    tid = sample_tenant.tenant_id
    db_session.add(
        Product(tenant_id=tid, name="Vela", sale_price_ars=Decimal("100"), stock_units=15)
    )
    await db_session.commit()
    archivo = await _importado(
        client, auth_headers, db_session, tid, [_fila("Vela", 10), _fila("Jabón", 4)]
    )
    ap = await aperturas_por_confirmar(db_session, tid)
    (fila,) = ap.por_archivo
    assert fila.aperturas == 1  # sólo el Jabón

    resp = await client.post(
        f"/api/v1/ingestion/files/{archivo.id}/opening-confirmation",
        json={"fecha": _iso(HACE_60)},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["aperturas"] == 1
    movs = {m.qty: m for m in await _aperturas_del_archivo(db_session, archivo.id)}
    assert movs[-5].opening_confirmed_at is None
    assert movs[-5].occurred_at is not None and movs[-5].occurred_at.date() == HOY


async def test_un_producto_dado_de_baja_no_bloquea_su_archivo(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Dar de baja un producto no anula sus movimientos: su apertura no puede
    seguir en el aviso ni frenar la confirmación del resto del archivo."""
    tid = sample_tenant.tenant_id
    archivo = await _importado(
        client, auth_headers, db_session, tid, [_fila("Vela", 10), _fila("Jabón", 7)]
    )
    jabon = (
        await db_session.execute(select(Product).where(Product.name == "Jabón"))
    ).scalar_one()
    jabon.is_active = False
    await db_session.commit()

    (fila,) = (await aperturas_por_confirmar(db_session, tid)).por_archivo
    assert fila.aperturas == 1
    resp = await client.post(
        f"/api/v1/ingestion/files/{archivo.id}/opening-confirmation",
        json={"fecha": _iso(HACE_60)},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["aperturas"] == 1
