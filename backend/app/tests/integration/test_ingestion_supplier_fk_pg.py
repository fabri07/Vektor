"""Integración PostgreSQL: un proveedor nuevo existe ANTES de que algo lo referencie.

Un libro de compras real con un proveedor que Véktor no conocía terminaba en 500:

    ForeignKeyViolationError: insert or update on table "inventory_movements"
    violates foreign key constraint "fk_inventory_movements_supplier_id"
    DETAIL: Key (supplier_id)=(...) is not present in table "suppliers".

`_resolve_or_create_supplier` agrega el `Supplier` a la sesión SIN flush —el id es
explícito, así que alcanza para setear la columna— y devuelve el id. Pero un id no
satisface una FK: la satisface la FILA. Y como `InventoryMovement` no declara
`relationship()` hacia `Supplier` (sólo la columna con `ForeignKey`), la
unit-of-work no tiene arista de dependencia y puede emitir el INSERT del
movimiento antes que el del proveedor.

**La suite normal no puede verlo**: SQLite no valida claves foráneas por default.
El mismo fenómeno ya estaba documentado en `test_ingestion_lease_pg.py`, donde un
flush manual lo esquiva dentro del test — pero el importador de producción no lo
hacía. Ver ``[[feedback_sqlite_masks_postgres]]``.

Gating: se **skippea limpio** sin ``TEST_PG_DSN``. Para correrlo::

    TEST_PG_DSN='postgresql+asyncpg://vektor:vektor@localhost:5432/vektor' \\
        pytest app/tests/integration/test_ingestion_supplier_fk_pg.py -v --no-cov
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.application.services.ingestion_import_service import (
    _record_stock_movement,
    _resolve_or_create_supplier,
)
from app.persistence.models.inventory import InventoryBalance, InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.supplier import Supplier
from app.persistence.models.tenant import Tenant

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]


@pytest_asyncio.fixture(scope="module")
async def pg_engine() -> AsyncGenerator[AsyncEngine, None]:
    assert TEST_PG_DSN is not None
    engine = create_async_engine(TEST_PG_DSN, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def tenant_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest_asyncio.fixture
async def sm(
    pg_engine: AsyncEngine, tenant_id: uuid.UUID
) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    # `autoflush=False` como producción: con autoflush el SELECT del índice de
    # balances materializaría al proveedor de casualidad y el test pasaría por un
    # motivo que producción no tiene.
    factory = async_sessionmaker(pg_engine, expire_on_commit=False, autoflush=False)
    try:
        yield factory
    finally:
        async with factory() as s:
            await s.execute(
                delete(InventoryMovement).where(InventoryMovement.tenant_id == tenant_id)
            )
            await s.execute(
                delete(InventoryBalance).where(InventoryBalance.tenant_id == tenant_id)
            )
            await s.execute(delete(Product).where(Product.tenant_id == tenant_id))
            await s.execute(delete(Supplier).where(Supplier.tenant_id == tenant_id))
            await s.execute(delete(Tenant).where(Tenant.tenant_id == tenant_id))
            await s.commit()


async def test_una_compra_de_un_proveedor_nuevo_no_rompe_la_fk(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """El orden real del importador: se resuelve el proveedor y se registra el
    movimiento de compra en la misma transacción, sin commit intermedio."""
    product_id = uuid.uuid4()
    async with sm() as session:
        session.add(Tenant(tenant_id=tenant_id, legal_name="T", display_name="T"))
        await session.flush()
        session.add(
            Product(
                id=product_id,
                tenant_id=tenant_id,
                name="Lavandina Ayudín 1L",
                sale_price_ars=Decimal("100.00"),
                stock_units=0,
            )
        )
        await session.flush()

        supplier_index: dict[str, uuid.UUID] = {}
        supplier_id, _ = await _resolve_or_create_supplier(
            session, tenant_id, "J. Perez Insumos", supplier_index, []
        )
        assert supplier_id is not None

        await _record_stock_movement(
            session,
            tenant_id,
            product_id,
            qty=24,
            unit_cost=Decimal("1629.64"),
            movement_type="purchase",
            final_qty=24,
            supplier_id=supplier_id,
            source_type="purchase_import",
        )
        # El commit es el que ejecuta los INSERT pendientes: acá reventaba.
        await session.commit()

    # Y el proveedor quedó de verdad, no sólo su id en memoria.
    async with sm() as s:
        guardado = (
            await s.execute(select(Supplier).where(Supplier.id == supplier_id))
        ).scalar_one_or_none()
        assert guardado is not None, "el proveedor referenciado por el movimiento no existe"
        movimiento = (
            await s.execute(
                select(InventoryMovement).where(InventoryMovement.tenant_id == tenant_id)
            )
        ).scalar_one()
        assert movimiento.supplier_id == supplier_id
