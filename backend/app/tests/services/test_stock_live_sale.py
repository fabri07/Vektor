"""``decrement_for_sale`` / ``revert_sale_stock``: descuento idempotente de venta en vivo.

Cubre el fix "la venta en vivo no descuenta stock" (chat + POST /sales no tocaban stock;
solo manual-batch lo hacía). El helper es el único punto de descuento y es idempotente
por ``source_event_id="sale:{id}"`` para que el path síncrono y el backstop por evento
``SALE_RECORDED`` no doble-descuenten.
"""

from __future__ import annotations

import unittest.mock
import uuid
from collections.abc import Generator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.stock_service import (
    InsufficientStockError,
    check_stock_available,
    decrement_for_sale,
    revert_sale_stock,
    sale_source_event_id,
    validate_sale_update_stock,
)
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry


@pytest.fixture(autouse=True)
def _no_event_bus() -> Generator[None, None, None]:
    # decrement_stock emite STOCK_DECREASED vía EventBus → Celery → Redis. Patchearlo
    # (mismo patrón que test_stock_service_occurred_at.py) para no depender del broker.
    with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
        yield


async def _make_product(db: AsyncSession, tenant_id: uuid.UUID, stock_units: int) -> Product:
    product = Product(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name="Coca 500ml",
        sale_price_ars=Decimal("1200"),
        unit_cost_ars=Decimal("700"),
        stock_units=stock_units,
    )
    db.add(product)
    await db.flush()
    return product


async def _make_sale(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    product_id: uuid.UUID | None,
    quantity: int,
) -> SaleEntry:
    sale = SaleEntry(
        tenant_id=tenant_id,
        amount=Decimal("1200"),
        quantity=quantity,
        product_id=product_id,
        transaction_date=datetime(2026, 7, 10, 12, 0, tzinfo=UTC),
        payment_method="cash",
    )
    db.add(sale)
    await db.flush()
    return sale


async def _count_live_sale_movements(db: AsyncSession, sale_id: uuid.UUID) -> int:
    return int(
        (
            await db.execute(
                select(func.count(InventoryMovement.id)).where(
                    InventoryMovement.source_event_id == sale_source_event_id(sale_id),
                    InventoryMovement.movement_type == "sale",
                    InventoryMovement.voided_at.is_(None),
                )
            )
        ).scalar_one()
    )


@pytest.mark.asyncio
async def test_decrement_for_sale_lowers_stock_and_creates_one_movement(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    product = await _make_product(db_session, tid, stock_units=10)
    sale = await _make_sale(db_session, tid, product.id, quantity=3)

    movement = await decrement_for_sale(sale, db_session)

    assert movement is not None
    assert movement.movement_type == "sale"
    assert movement.qty == -3
    assert movement.source_event_id == sale_source_event_id(sale.id)
    assert movement.occurred_at == sale.transaction_date
    assert product.stock_units == 7
    assert await _count_live_sale_movements(db_session, sale.id) == 1


@pytest.mark.asyncio
async def test_decrement_for_sale_is_idempotent(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Sync + backstop por evento: dos llamadas descuentan UNA sola vez."""
    tid = sample_tenant.tenant_id
    product = await _make_product(db_session, tid, stock_units=10)
    sale = await _make_sale(db_session, tid, product.id, quantity=4)

    first = await decrement_for_sale(sale, db_session)
    second = await decrement_for_sale(sale, db_session)

    assert first is not None
    assert second is None  # no-op
    assert product.stock_units == 6  # descontado una sola vez
    assert await _count_live_sale_movements(db_session, sale.id) == 1


@pytest.mark.asyncio
async def test_decrement_for_sale_noop_without_product(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    sale = await _make_sale(db_session, tid, product_id=None, quantity=2)

    assert await decrement_for_sale(sale, db_session) is None


@pytest.mark.asyncio
async def test_decrement_for_sale_noop_when_voided(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    product = await _make_product(db_session, tid, stock_units=5)
    sale = await _make_sale(db_session, tid, product.id, quantity=2)
    sale.voided_at = datetime.now(UTC)
    sale.void_reason = "MANUAL_ADMIN_VOID"
    await db_session.flush()

    assert await decrement_for_sale(sale, db_session) is None
    assert product.stock_units == 5


@pytest.mark.asyncio
async def test_revert_sale_stock_restores_and_returns_bool(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    product = await _make_product(db_session, tid, stock_units=10)
    sale = await _make_sale(db_session, tid, product.id, quantity=3)

    await decrement_for_sale(sale, db_session)
    assert product.stock_units == 7

    assert await revert_sale_stock(sale.id, tid, db_session) is True  # había movimiento
    assert product.stock_units == 10  # repuesto (incremental)
    assert await _count_live_sale_movements(db_session, sale.id) == 0

    # Segundo revert: no-op, devuelve False (ya no hay movimiento vivo).
    assert await revert_sale_stock(sale.id, tid, db_session) is False
    assert product.stock_units == 10


@pytest.mark.asyncio
async def test_revert_returns_false_for_sale_without_movement(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Venta histórica importada (product_id pero SIN movimiento): revert devuelve False
    → el PATCH no arranca a descontar stock que nunca se descontó."""
    tid = sample_tenant.tenant_id
    product = await _make_product(db_session, tid, stock_units=10)
    sale = await _make_sale(db_session, tid, product.id, quantity=3)  # sin decrement_for_sale

    assert await revert_sale_stock(sale.id, tid, db_session) is False
    assert product.stock_units == 10  # intacto


@pytest.mark.asyncio
async def test_decrement_for_sale_rejects_when_insufficient_stock(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """NO se permite stock negativo: venta EN VIVO > stock levanta InsufficientStockError
    y NO crea movimiento ni toca el stock (stock 3, venta 10)."""
    tid = sample_tenant.tenant_id
    product = await _make_product(db_session, tid, stock_units=3)
    sale = await _make_sale(db_session, tid, product.id, quantity=10)

    with pytest.raises(InsufficientStockError):
        await decrement_for_sale(sale, db_session)

    assert product.stock_units == 3  # intacto
    assert await _count_live_sale_movements(db_session, sale.id) == 0


@pytest.mark.asyncio
async def test_check_stock_available_passes_and_raises(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    product = await _make_product(db_session, tid, stock_units=5)

    # Suficiente (incluye el borde exacto): devuelve el producto bloqueado.
    assert (await check_stock_available(product.id, tid, 5, db_session)).id == product.id
    # Insuficiente: levanta con los valores disponibles/pedidos.
    with pytest.raises(InsufficientStockError) as exc:
        await check_stock_available(product.id, tid, 6, db_session)
    assert exc.value.available == 5
    assert exc.value.requested == 6


@pytest.mark.asyncio
async def test_validate_sale_update_stock_incremental(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """El validador de PATCH modela el efecto neto: subir la cantidad solo necesita el
    delta (repone la cantidad vieja del mismo producto antes de exigir la nueva)."""
    tid = sample_tenant.tenant_id
    product = await _make_product(db_session, tid, stock_units=10)
    sale = await _make_sale(db_session, tid, product.id, quantity=3)
    await decrement_for_sale(sale, db_session)  # stock → 7, movimiento vivo de 3

    # Subir de 3 a 10: repone 3 → disponible 7+3=10, alcanza justo. No levanta.
    await validate_sale_update_stock(sale.id, tid, product.id, 3, product.id, 10, db_session)
    # Subir de 3 a 11: 7+3=10 < 11 → rechaza.
    with pytest.raises(InsufficientStockError):
        await validate_sale_update_stock(sale.id, tid, product.id, 3, product.id, 11, db_session)
    # Desasociar el producto (new_product_id None) → solo repone, nunca falta: no-op.
    await validate_sale_update_stock(sale.id, tid, product.id, 3, None, 0, db_session)


@pytest.mark.asyncio
async def test_chat_register_sale_insufficient_stock_raises_and_creates_nothing(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Chat REGISTER_SALE con producto sin stock: levanta InsufficientStockError (mensaje
    claro para el usuario) y, con el rollback del savepoint de _execute_local_action, NO
    queda ninguna venta."""
    from app.application.agents.shared.schemas import ActionType  # noqa: PLC0415
    from app.application.services.pending_action_service import (  # noqa: PLC0415
        execute_pending_action,
    )
    from app.application.services.stock_service import (  # noqa: PLC0415
        INSUFFICIENT_STOCK_MESSAGE,
    )
    from app.persistence.models.pending_action import PendingAction  # noqa: PLC0415

    tid = sample_tenant.tenant_id
    product = await _make_product(db_session, tid, stock_units=2)

    action = PendingAction()
    action.id = uuid.uuid4()
    action.tenant_id = tid
    action.user_id = uuid.uuid4()
    action.action_type = ActionType.REGISTER_SALE
    action.payload = {
        "amount": "1200",
        "payment_method": "cash",
        "product_id": str(product.id),
        "quantity": 5,
        "transaction_date": "2026-07-10",
    }
    action.risk_level = "MEDIUM"
    action.status = "APPROVED"
    action.external_system = None

    # Espeja el savepoint de _execute_local_action (agent.py): el rollback deja 0 ventas.
    with pytest.raises(InsufficientStockError) as exc:
        async with db_session.begin_nested():
            await execute_pending_action(action, db_session)
    assert exc.value.user_message == INSUFFICIENT_STOCK_MESSAGE

    remaining = (
        await db_session.execute(
            select(func.count(SaleEntry.id)).where(
                SaleEntry.tenant_id == tid,
                SaleEntry.product_id == product.id,
            )
        )
    ).scalar_one()
    assert remaining == 0
    assert product.stock_units == 2  # intacto


@pytest.mark.asyncio
async def test_decrement_for_sale_no_se_traga_violaciones_ajenas(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Una IntegrityError que NO es la carrera de idempotencia se re-propaga.

    Antes de F5-B acá había un ``except IntegrityError`` a secas: cualquier violación
    —la del unique nuevo de ``inventory_balances``, una FK, un NOT NULL— salía como
    ``None``, o sea "no había nada que descontar". El resultado era una venta
    persistida que nunca descuenta stock, sin log ni error, detectable recién semanas
    después como divergencia sin causa en el chequeo semanal de integridad.

    Se simula con una violación arbitraria dentro del savepoint (no la del índice
    vigilado) y se exige que SALGA, en vez de convertirse en un no-op silencioso.
    """
    tid = sample_tenant.tenant_id
    product = await _make_product(db_session, tid, stock_units=10)
    sale = await _make_sale(db_session, tid, product.id, quantity=3)

    async def _boom(**kwargs: object) -> InventoryMovement:
        raise IntegrityError(
            "INSERT INTO inventory_balances ...",
            {},
            Exception('null value in column "tenant_id" violates not-null constraint'),
        )

    with (
        unittest.mock.patch(
            "app.application.services.stock_service.decrement_stock", side_effect=_boom
        ),
        pytest.raises(IntegrityError),
    ):
        await decrement_for_sale(sale, db_session)

    await db_session.rollback()
