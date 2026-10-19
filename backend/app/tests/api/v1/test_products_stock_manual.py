"""M·P2c: el stock cargado a mano queda igual que un dato importado.

- El alta con stock deja la MISMA apertura que un catálogo importado en «ya la
  tenía» (``adjustment`` + ``catalog_initial_stock``, con su costo).
- La edición del stock deja un ajuste manual (``manual_adjustment``) por la
  diferencia, nunca un ``setattr`` absoluto ni una apertura.
- El precio de compra es obligatorio cuando entra stock; el envío, no.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import costo_promedio_service as svc
from app.application.services.ingestion_import_service import _apply_catalog_stock
from app.application.services.inventory_integrity_service import (
    check_tenant_inventory_integrity,
)
from app.application.services.inventory_movement_origin import (
    SOURCE_CATALOG_INITIAL_STOCK,
    SOURCE_MANUAL_ADJUSTMENT,
)
from app.application.services.product_dedup_service import (
    STOCK_MOST_RECENT,
    MovementRow,
    ProductRecord,
    classify_group_stock_decision,
)
from app.domain.costo_promedio import proyectar
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant

_ALTA = {
    "name": "Vela aromática",
    "sale_price_ars": "300.00",
    "unit_cost_ars": "80.00",
    "stock_units": 50, "acquired_at": "2026-01-01T09:00:00",
}


@pytest.fixture(autouse=True)
def _sin_celery(mock_score_trigger: Any) -> None:
    pass


async def _movimientos(db: AsyncSession, product_id: uuid.UUID) -> list[InventoryMovement]:
    return list(
        (
            await db.execute(
                select(InventoryMovement)
                .where(InventoryMovement.product_id == product_id)
                .order_by(InventoryMovement.created_at)
            )
        )
        .scalars()
        .all()
    )


async def _alta(client: AsyncClient, headers: dict[str, Any], **extra: Any) -> dict[str, Any]:
    resp = await client.post("/api/v1/products", json={**_ALTA, **extra}, headers=headers)
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def _ultimo_estado(db: AsyncSession, tenant_id: uuid.UUID, pid: uuid.UUID) -> Any:
    """El estado después del último evento (los registros materializados sólo
    guardan los que cambian algo: una entrada al mismo costo no deja uno)."""
    calculo = await svc.calcular(db, tenant_id, [pid])
    h = calculo.historias[pid]
    assert not h.stock_fuera_del_ledger
    return proyectar(h.eventos, aproximado=h.aproximado)[-1][1]


# ── Alta ──────────────────────────────────────────────────────────────────────


async def test_alta_con_stock_escribe_la_apertura_del_import(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    data = await _alta(client, auth_headers)
    pid = uuid.UUID(data["id"])
    assert data["stock_units"] == 50

    (mov,) = await _movimientos(db_session, pid)
    assert (mov.movement_type, mov.source_type, mov.qty) == (
        "adjustment",
        SOURCE_CATALOG_INITIAL_STOCK,
        50,
    )
    assert mov.unit_cost == Decimal("80.00")
    assert mov.unit_cost_final == Decimal("80.00")
    assert mov.shipping_treatment == "desconocido"

    # El mismo stock de un catálogo importado en «ya la tenía».
    importado = Product(
        tenant_id=tid, name="Vela importada", sale_price_ars=Decimal("300"), stock_units=50
    )
    db_session.add(importado)
    await db_session.flush()
    await _apply_catalog_stock(
        db_session,
        tid,
        product_id=importado.id,
        product_name=importado.name,
        delta=50,
        final_qty=50,
        unit_cost=Decimal("80.00"),
        store_name=None,
        tx_date=datetime.now(UTC),
        uploaded_file_id=None,
        source_row_ref=None,
        balance_index=None,
    )
    (mov_imp,) = await _movimientos(db_session, importado.id)
    assert (mov_imp.movement_type, mov_imp.source_type) == (mov.movement_type, mov.source_type)

    manual = await _ultimo_estado(db_session, tid, pid)
    imp = await _ultimo_estado(db_session, tid, importado.id)
    assert manual.estado == imp.estado == "medido"
    assert manual.promedio == imp.promedio == Decimal("80.00")
    assert manual.u_conocidas == imp.u_conocidas == 50


async def test_alta_con_stock_sin_precio_de_compra_da_422(
    client: AsyncClient, auth_headers: dict[str, Any]
) -> None:
    payload = {**_ALTA, "unit_cost_ars": None}
    resp = await client.post("/api/v1/products", json=payload, headers=auth_headers)
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "COSTO_REQUERIDO_CON_STOCK"


async def test_alta_sin_stock_no_exige_costo_ni_escribe_movimiento(
    client: AsyncClient, auth_headers: dict[str, Any], db_session: AsyncSession
) -> None:
    data = await _alta(client, auth_headers, unit_cost_ars=None, stock_units=0)
    assert await _movimientos(db_session, uuid.UUID(data["id"])) == []


async def test_alta_guarda_el_envio_declarado(
    client: AsyncClient, auth_headers: dict[str, Any], db_session: AsyncSession
) -> None:
    data = await _alta(client, auth_headers, costo_envio="incluido")
    (mov,) = await _movimientos(db_session, uuid.UUID(data["id"]))
    assert mov.shipping_treatment == "incluido"


async def test_alta_reintentada_con_la_misma_clave_deja_un_solo_movimiento(
    client: AsyncClient, auth_headers: dict[str, Any], db_session: AsyncSession
) -> None:
    headers = {**auth_headers, "Idempotency-Key": "alta-vela-1"}
    primera = await client.post("/api/v1/products", json=_ALTA, headers=headers)
    assert primera.status_code == 201
    segunda = await client.post("/api/v1/products", json=_ALTA, headers=headers)
    assert segunda.status_code == 409
    movs = (
        await db_session.execute(
            select(InventoryMovement).where(InventoryMovement.source_type.is_not(None))
        )
    ).scalars().all()
    assert len(movs) == 1


# ── Edición ───────────────────────────────────────────────────────────────────


async def test_suba_por_correccion_es_ajuste_manual_con_costo_y_queda_medido(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    pid = (await _alta(client, auth_headers))["id"]
    resp = await client.patch(
        f"/api/v1/products/{pid}",
        json={"stock_units": 60, "stock_units_esperado": 50, "costo_envio": "no_aplica"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["stock_units"] == 60

    _, ajuste = await _movimientos(db_session, uuid.UUID(pid))
    assert (ajuste.movement_type, ajuste.source_type, ajuste.qty) == (
        "adjustment",
        SOURCE_MANUAL_ADJUSTMENT,
        10,
    )
    assert ajuste.unit_cost_final == Decimal("80.00")
    assert ajuste.shipping_treatment == "no_aplica"

    estado = await _ultimo_estado(db_session, sample_tenant.tenant_id, uuid.UUID(pid))
    assert estado.estado == "medido"
    assert estado.u_conocidas == 60
    assert estado.u_sin_costo == 0


async def test_baja_es_salida_sin_costo(
    client: AsyncClient, auth_headers: dict[str, Any], db_session: AsyncSession
) -> None:
    pid = (await _alta(client, auth_headers))["id"]
    resp = await client.patch(
        f"/api/v1/products/{pid}", json={"stock_units": 45}, headers=auth_headers
    )
    assert resp.status_code == 200
    _, ajuste = await _movimientos(db_session, uuid.UUID(pid))
    assert (ajuste.source_type, ajuste.qty, ajuste.unit_cost) == (
        SOURCE_MANUAL_ADJUSTMENT,
        -5,
        None,
    )


async def test_stock_que_cambio_mientras_editaba_da_409_y_no_pisa(
    client: AsyncClient, auth_headers: dict[str, Any], db_session: AsyncSession
) -> None:
    pid = (await _alta(client, auth_headers))["id"]
    resp = await client.patch(
        f"/api/v1/products/{pid}",
        json={"stock_units": 70, "stock_units_esperado": 48},
        headers=auth_headers,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "STOCK_CAMBIO"
    assert len(await _movimientos(db_session, uuid.UUID(pid))) == 1


async def test_suba_en_producto_sin_costo_da_422(
    client: AsyncClient, auth_headers: dict[str, Any]
) -> None:
    pid = (await _alta(client, auth_headers, unit_cost_ars=None, stock_units=0))["id"]
    resp = await client.patch(
        f"/api/v1/products/{pid}", json={"stock_units": 5}, headers=auth_headers
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "COSTO_REQUERIDO_CON_STOCK"
    # Con el precio en el mismo PATCH, entra.
    ok = await client.patch(
        f"/api/v1/products/{pid}",
        json={"stock_units": 5, "unit_cost_ars": "90.00"},
        headers=auth_headers,
    )
    assert ok.status_code == 200


async def test_patch_sin_cambio_de_stock_no_escribe_movimiento(
    client: AsyncClient, auth_headers: dict[str, Any], db_session: AsyncSession
) -> None:
    pid = (await _alta(client, auth_headers))["id"]
    resp = await client.patch(
        f"/api/v1/products/{pid}", json={"sale_price_ars": "320.00"}, headers=auth_headers
    )
    assert resp.status_code == 200
    resp = await client.patch(
        f"/api/v1/products/{pid}", json={"stock_units": 50}, headers=auth_headers
    )
    assert resp.status_code == 200
    assert len(await _movimientos(db_session, uuid.UUID(pid))) == 1


async def test_integridad_no_ve_divergencia_con_alta_y_ajustes_manuales(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    pid = (await _alta(client, auth_headers))["id"]
    for nuevo in (80, 30):
        resp = await client.patch(
            f"/api/v1/products/{pid}", json={"stock_units": nuevo}, headers=auth_headers
        )
        assert resp.status_code == 200
    reporte = await check_tenant_inventory_integrity(db_session, sample_tenant.tenant_id)
    assert reporte["checked"] >= 1
    assert reporte["divergences"] == []
    assert reporte["skipped_complex_ledger"] == 0


def test_la_deduplicacion_no_toma_un_ajuste_manual_como_ancla() -> None:
    """Un duplicado con apertura + corrección manual no es «catálogo puro»: si el
    ajuste fuera ``catalog_initial_stock``, MOST_RECENT tomaría el +5 como el
    stock absoluto del grupo."""
    ahora = datetime(2026, 10, 14, tzinfo=UTC)

    def _registro(stock: int, *movs: MovementRow) -> ProductRecord:
        return ProductRecord(
            id=uuid.uuid4(), has_user_edits=False, barcode_normalized=None,
            sku_normalized=None, created_at=ahora, unit_cost_ars=Decimal("80"),
            stock_units=stock, fk_count=0, current_qty=stock, reserved_qty=0,
            movements=movs,
        )

    def _mov(qty: int, origen: str) -> MovementRow:
        return MovementRow(
            qty=qty, movement_type="adjustment", source_type=origen,
            source_row_hash=None, occurred_at=ahora, created_at=ahora,
        )

    canonico = _registro(50, _mov(50, SOURCE_CATALOG_INITIAL_STOCK))
    duplicado = _registro(
        35, _mov(30, SOURCE_CATALOG_INITIAL_STOCK), _mov(5, SOURCE_MANUAL_ADJUSTMENT)
    )
    decision = classify_group_stock_decision(canonico, [duplicado])
    assert decision.kind != STOCK_MOST_RECENT
