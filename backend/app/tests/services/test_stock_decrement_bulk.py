"""F-F.3.b — el descuento por lote escribe lo mismo que el de a una, con menos viajes.

Aplicar la historia de un archivo costaba ~4 sentencias y un envío al broker **por
venta**: sobre el archivo real (1.187 ventas) son ~4.700 sentencias adentro del
request del confirm. El lote hace que el costo dependa de la cantidad de
**productos** y no de la de ventas.

Lo que se prueba acá es lo que el ahorro no puede romper:

- que el saldo final es idéntico al que dejaba el camino de a una, incluido el
  **clamp paso a paso** (``stock_units`` no baja de cero, así que una venta que
  pasa por el piso deja las siguientes restando desde 0 — colapsarlo en un solo
  ``max(0, ...)`` sobre el total daría otro número justo en el caso que el clamp
  existe para cubrir);
- que ``current_qty`` del balance NO se clampa, porque el balance registra el saldo
  real y ahí un negativo es información, no un error a tapar;
- que la **carrera** —una venta en vivo que se descuenta entre el pre-chequeo y el
  INSERT del lote— no descuenta dos veces: el lote se rehace de a una y la fila
  conflictiva cuenta como ya aplicada;
- que el aviso al broker se emite una vez por corrida y no una por venta.

El recorrido completo (confirm → stock → borrado que revierte) vive en
``app/tests/api/v1/test_ingestion_confirm_aplica_replay.py``; acá está el núcleo.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agents.shared.event_bus import EventBus
from app.application.services import stock_service
from app.application.services.stock_service import (
    BulkDecrementItem,
    decrement_stock_bulk,
    sale_source_event_id,
)
from app.persistence.models.inventory import InventoryBalance, InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant

_STOCK_PREVIO = 10


@pytest.fixture
def eventos(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """Captura lo que se manda al broker sin mandarlo."""
    capturados: list[tuple[str, dict[str, Any]]] = []

    def _emit(event_type: str, payload: dict[str, Any]) -> None:
        capturados.append((event_type, payload))

    monkeypatch.setattr(EventBus, "emit", staticmethod(_emit))
    return capturados


@pytest_asyncio.fixture
async def producto(db_session: AsyncSession, sample_tenant: Tenant) -> Product:
    registro = Product(
        id=uuid.uuid4(),
        tenant_id=sample_tenant.tenant_id,
        name="Vela aromatica 200g",
        sale_price_ars=Decimal("2100"),
        unit_cost_ars=Decimal("1200"),
        stock_units=_STOCK_PREVIO,
    )
    db_session.add(registro)
    await db_session.commit()
    return registro


def _item(producto: Product, qty: int, sale_id: uuid.UUID | None = None) -> BulkDecrementItem:
    return BulkDecrementItem(
        product=producto,
        qty=qty,
        source_event_id=sale_source_event_id(sale_id or uuid.uuid4()),
    )


async def _balance(db_session: AsyncSession, producto: Product) -> InventoryBalance:
    return (
        await db_session.execute(
            select(InventoryBalance).where(InventoryBalance.product_id == producto.id)
        )
    ).scalar_one()


#: El índice que hace cumplir la idempotencia del descuento de venta. Lo crea la
#: migración `20260729_0001` y **sólo en Postgres**: el schema de los tests sale de
#: `Base.metadata.create_all`, que no lo conoce. Sin él, en SQLite el INSERT
#: duplicado entra y el camino de colisión no existiría — o sea que el test pasaría
#: sin ejercitar nada. Se crea acá, con el mismo predicado que la migración, para
#: que el caso corra contra la condición real y no contra una más permisiva.
_INDICE_IDEMPOTENCIA = (
    "CREATE UNIQUE INDEX uq_inventory_movements_live_sale_event "
    "ON inventory_movements (tenant_id, source_event_id) "
    "WHERE movement_type = 'sale' AND voided_at IS NULL AND source_event_id LIKE 'sale:%'"
)


async def _crear_indice_de_idempotencia(db_session: AsyncSession) -> None:
    await db_session.execute(text(_INDICE_IDEMPOTENCIA))


async def _movimientos(db_session: AsyncSession, producto: Product) -> list[InventoryMovement]:
    return list(
        (
            await db_session.execute(
                select(InventoryMovement).where(InventoryMovement.product_id == producto.id)
            )
        )
        .scalars()
        .all()
    )


class TestElLoteDejaElMismoSaldoQueElCaminoDeAUna:
    async def test_varias_ventas_del_mismo_producto_se_acumulan(
        self,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        producto: Product,
        eventos: list[tuple[str, dict[str, Any]]],
    ) -> None:
        outcome = await decrement_stock_bulk(
            sample_tenant.tenant_id,
            [_item(producto, 2), _item(producto, 3), _item(producto, 1)],
            db_session,
        )
        await db_session.commit()

        assert outcome.applied == 3
        assert outcome.already_applied == 0
        await db_session.refresh(producto)
        assert producto.stock_units == _STOCK_PREVIO - 6
        assert (await _balance(db_session, producto)).current_qty == _STOCK_PREVIO - 6
        # Un movimiento por venta: el lote ahorra viajes, no traza.
        movimientos = await _movimientos(db_session, producto)
        assert len(movimientos) == 3
        assert sorted(m.qty for m in movimientos) == [-3, -2, -1]

    async def test_el_clamp_de_stock_units_se_aplica_paso_a_paso(
        self,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        producto: Product,
        eventos: list[tuple[str, dict[str, Any]]],
    ) -> None:
        """Dos ventas de 8 sobre un stock de 10.

        De a una: ``max(0, 10-8)=2`` y después ``max(0, 2-8)=0``.
        Acumulando y clampando al final: ``max(0, 10-16)`` también da 0 — así que el
        caso se elige para que los dos caminos NO coincidan: con una tercera venta
        el resultado sólo es el mismo si el clamp corre en cada paso.
        """
        await decrement_stock_bulk(
            sample_tenant.tenant_id,
            [_item(producto, 8), _item(producto, 8), _item(producto, 8)],
            db_session,
        )
        await db_session.commit()

        await db_session.refresh(producto)
        assert producto.stock_units == 0
        # El balance NO se clampa: 10 - 24 = -14. Un saldo negativo acá es el dato de
        # que la historia del archivo no cierra, y taparlo lo volvería inencontrable.
        assert (await _balance(db_session, producto)).current_qty == _STOCK_PREVIO - 24

    async def test_sin_items_no_toca_nada_ni_avisa(
        self,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        producto: Product,
        eventos: list[tuple[str, dict[str, Any]]],
    ) -> None:
        outcome = await decrement_stock_bulk(sample_tenant.tenant_id, [], db_session)
        assert (outcome.applied, outcome.already_applied) == (0, 0)
        assert eventos == []


class TestLaCarreraNoDescuentaDosVeces:
    async def test_una_venta_ya_descontada_hace_que_el_lote_se_rehaga_de_a_una(
        self,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        producto: Product,
        eventos: list[tuple[str, dict[str, Any]]],
    ) -> None:
        """El movimiento ya existe cuando el lote intenta insertarlo.

        Es la carrera real: entre el pre-chequeo del caller (``_ya_descontadas``) y
        este INSERT, una venta en vivo descontó el mismo registro. El lote entero
        choca contra el índice único y se rehace de a una — las otras dos ventas
        entran igual y la conflictiva cuenta como ya aplicada, no como aplicada.
        """
        await _crear_indice_de_idempotencia(db_session)
        ya_vendida = uuid.uuid4()
        await stock_service.decrement_stock(
            product_id=producto.id,
            tenant_id=sample_tenant.tenant_id,
            qty=1,
            source_event_id=sale_source_event_id(ya_vendida),
            db=db_session,
        )
        await db_session.commit()
        stock_tras_la_venta_en_vivo = int(producto.stock_units)

        outcome = await decrement_stock_bulk(
            sample_tenant.tenant_id,
            [_item(producto, 2), _item(producto, 1, sale_id=ya_vendida), _item(producto, 3)],
            db_session,
        )
        await db_session.commit()

        assert outcome.applied == 2
        assert outcome.already_applied == 1
        await db_session.refresh(producto)
        # Descuenta 2+3, NO la que ya estaba: si el fallback no existiera, o bien se
        # perdería todo el lote o bien la venta en vivo se descontaría dos veces.
        assert producto.stock_units == stock_tras_la_venta_en_vivo - 5
        movimientos = await _movimientos(db_session, producto)
        assert len(movimientos) == 3, "no se creó un segundo movimiento para la misma venta"
        claves = [m.source_event_id for m in movimientos]
        assert claves.count(sale_source_event_id(ya_vendida)) == 1


class TestElAvisoAlBrokerNoEscalaConLasVentas:
    async def test_un_stock_decreased_por_corrida_y_no_uno_por_venta(
        self,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        producto: Product,
        eventos: list[tuple[str, dict[str, Any]]],
    ) -> None:
        """``events.stock_decreased`` sólo encola el recálculo de score del tenant.

        Emitirlo por venta encolaba, sobre el archivo real, 1.187 veces el mismo
        recálculo del mismo negocio.
        """
        await decrement_stock_bulk(
            sample_tenant.tenant_id,
            [_item(producto, 1) for _ in range(5)],
            db_session,
        )
        await db_session.commit()

        assert [e for e, _ in eventos].count("STOCK_DECREASED") == 1

    async def test_una_alerta_por_producto_que_queda_bajo_el_umbral(
        self,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        producto: Product,
        eventos: list[tuple[str, dict[str, Any]]],
    ) -> None:
        """Y no una por cada venta posterior al cruce.

        Con el umbral por defecto, un producto con cuarenta ventas que cruzaba en la
        doceava emitía veintinueve alertas idénticas.
        """
        await decrement_stock_bulk(
            sample_tenant.tenant_id,
            [_item(producto, 1) for _ in range(_STOCK_PREVIO)],
            db_session,
        )
        await db_session.commit()

        alertas = [p for e, p in eventos if e == "STOCK_ALERT_CREATED"]
        assert len(alertas) == 1
        assert alertas[0]["product_id"] == str(producto.id)
        assert alertas[0]["current_qty"] == 0
