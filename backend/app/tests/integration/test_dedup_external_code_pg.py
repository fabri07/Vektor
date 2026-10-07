"""B·3 contra PostgreSQL real — mover el código externo al fusionar respeta el índice.

``uq_products_tenant_external_code`` es parcial sobre ACTIVOS y Postgres lo chequea
fila por fila, en el momento del UPDATE, no al final de la transacción. SQLite no
reproduce eso con fidelidad (ver ``[[feedback_sqlite_masks_postgres]]``), así que
acá se prueba lo único que importa del orden de escritura:

- el control: escribir en el destino y liberar el origen en el MISMO flush viola
  el índice cuando SQLAlchemy ordena primero el destino (lo hace por clave
  primaria, no por intención) — prueba que el escenario no es vacío;
- ``_move_external_code`` (dos flushes: libera, después escribe) no lo viola, en
  los dos sentidos (apply: duplicado → canónico; reversa: canónico → duplicado),
  con los dos productos ACTIVOS, que es el peor caso;
- después de mover, el código sigue siendo único: otro producto no lo puede tomar,
  ni en una transacción concurrente que espera al commit.

No necesita el plan de dedup completo: un grupo de dedup exige dos activos con el
mismo SKU, que los índices de F5-B ya no permiten crear en Postgres. Lo que hay que
probar contra el motor real es la transferencia; el flujo completo lo cubre
``test_product_dedup_external_code_b3.py`` en SQLite.

Gating: se skippea limpio sin ``TEST_PG_DSN``.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator
from decimal import Decimal
from typing import cast

import pytest
import pytest_asyncio
from sqlalchemy import Table, delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.application.services.product_dedup_service import _move_external_code
from app.persistence.db.base import Base
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]

def _id_with_prefix(prefix: str) -> uuid.UUID:
    """Id aleatorio (no choca entre corridas ni workers) con el prefijo fijado: el
    orden de los UPDATE de un flush sale de la clave primaria, así que el prefijo
    decide cuál de los dos se escribe primero."""
    return uuid.UUID(prefix + uuid.uuid4().hex[len(prefix) :])


_LOW_PREFIX, _HIGH_PREFIX = "00000000", "ffffffff"


@pytest_asyncio.fixture(scope="module")
async def pg_engine() -> AsyncGenerator[AsyncEngine, None]:
    assert TEST_PG_DSN is not None
    engine = create_async_engine(TEST_PG_DSN, poolclass=NullPool)
    tables: list[Table] = [cast("Table", Tenant.__table__), cast("Table", Product.__table__)]
    async with engine.begin() as conn:
        # checkfirst: con `alembic upgrade head` el schema ya existe y esto es no-op.
        await conn.run_sync(Base.metadata.create_all, tables=tables, checkfirst=True)
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
    # autoflush=False, como producción (`app/persistence/db/session.py`).
    maker = async_sessionmaker(pg_engine, expire_on_commit=False, autoflush=False)
    async with maker() as s:
        s.add(Tenant(tenant_id=tenant_id, legal_name="T", display_name="T"))
        await s.commit()
    try:
        yield maker
    finally:
        async with maker() as s:
            await s.execute(delete(Product).where(Product.tenant_id == tenant_id))
            await s.execute(delete(Tenant).where(Tenant.tenant_id == tenant_id))
            await s.commit()


async def _seed(
    maker: async_sessionmaker[AsyncSession],
    tid: uuid.UUID,
    *,
    holder_id: uuid.UUID,
    receiver_id: uuid.UUID,
) -> None:
    """``holder`` tiene el código, ``receiver`` no; los dos ACTIVOS."""
    async with maker() as s:
        s.add_all(
            [
                Product(
                    id=holder_id,
                    tenant_id=tid,
                    name="Tiene código",
                    sale_price_ars=Decimal("100"),
                    stock_units=0,
                    external_code="A-001",
                    external_source="tango",
                ),
                Product(
                    id=receiver_id,
                    tenant_id=tid,
                    name="Sin código",
                    sale_price_ars=Decimal("100"),
                    stock_units=0,
                ),
            ]
        )
        await s.commit()


async def _codes(
    maker: async_sessionmaker[AsyncSession], tid: uuid.UUID
) -> dict[uuid.UUID, str | None]:
    async with maker() as s:
        rows = (
            await s.execute(
                select(Product.id, Product.external_code_key).where(Product.tenant_id == tid)
            )
        ).all()
    return {r[0]: r[1] for r in rows}


async def test_control_un_solo_flush_viola_el_indice(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """Sin este control, el test de abajo podría pasar porque el ORM justo ordenó
    bien los UPDATE, no porque el código lo garantice."""
    tid = tenant_id
    high, low = _id_with_prefix(_HIGH_PREFIX), _id_with_prefix(_LOW_PREFIX)
    await _seed(sm, tid, holder_id=high, receiver_id=low)
    async with sm() as s:
        holder = await s.get(Product, high)
        receiver = await s.get(Product, low)
        assert holder is not None and receiver is not None
        receiver.external_code, receiver.external_source = "A-001", "tango"
        holder.external_code, holder.external_source = None, None
        with pytest.raises(IntegrityError, match="uq_products_tenant_external_code"):
            await s.commit()


@pytest.mark.parametrize(
    ("holder_prefix", "receiver_prefix"),
    [(_HIGH_PREFIX, _LOW_PREFIX), (_LOW_PREFIX, _HIGH_PREFIX)],
    ids=["destino-primero-por-pk", "origen-primero-por-pk"],
)
async def test_mover_el_codigo_no_viola_el_indice(
    sm: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    holder_prefix: str,
    receiver_prefix: str,
) -> None:
    tid = tenant_id
    holder_id = _id_with_prefix(holder_prefix)
    receiver_id = _id_with_prefix(receiver_prefix)
    await _seed(sm, tid, holder_id=holder_id, receiver_id=receiver_id)
    async with sm() as s:
        holder = await s.get(Product, holder_id)
        receiver = await s.get(Product, receiver_id)
        await _move_external_code(s, holder, receiver, "A-001", "tango")
        await s.commit()

    codes = await _codes(sm, tid)
    assert codes[holder_id] is None
    assert codes[receiver_id] is not None

    # Y de vuelta (la reversa), con los dos activos.
    async with sm() as s:
        holder = await s.get(Product, holder_id)
        receiver = await s.get(Product, receiver_id)
        await _move_external_code(s, receiver, holder, "A-001", "tango")
        await s.commit()

    codes = await _codes(sm, tid)
    assert codes[holder_id] is not None
    assert codes[receiver_id] is None


async def test_despues_de_mover_el_codigo_sigue_siendo_unico(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    tid = tenant_id
    high, low = _id_with_prefix(_HIGH_PREFIX), _id_with_prefix(_LOW_PREFIX)
    await _seed(sm, tid, holder_id=high, receiver_id=low)
    async with sm() as s:
        await _move_external_code(
            s, await s.get(Product, high), await s.get(Product, low), "A-001", "tango"
        )
        await s.commit()

    with pytest.raises(IntegrityError, match="uq_products_tenant_external_code"):
        async with sm() as s:
            s.add(
                Product(
                    tenant_id=tid,
                    name="Intruso",
                    sale_price_ars=Decimal("1"),
                    stock_units=0,
                    external_code="A-001",
                    external_source="tango",
                )
            )
            await s.commit()


async def test_transaccion_concurrente_espera_y_choca(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """Mientras la fusión tiene el código movido SIN commitear, un import que quiere
    dar de alta otro producto con el mismo código espera al commit y choca: el
    índice cubre la ventana de la transacción, no sólo el estado final."""
    tid = tenant_id
    high, low = _id_with_prefix(_HIGH_PREFIX), _id_with_prefix(_LOW_PREFIX)
    await _seed(sm, tid, holder_id=high, receiver_id=low)

    async with sm() as fusion:
        await _move_external_code(
            fusion,
            await fusion.get(Product, high),
            await fusion.get(Product, low),
            "A-001",
            "tango",
        )

        async def _alta_concurrente() -> None:
            async with sm() as other:
                await other.execute(text("SET LOCAL lock_timeout = '10s'"))
                other.add(
                    Product(
                        tenant_id=tid,
                        name="Concurrente",
                        sale_price_ars=Decimal("1"),
                        stock_units=0,
                        external_code="A-001",
                        external_source="tango",
                    )
                )
                await other.commit()

        task = asyncio.create_task(_alta_concurrente())
        await asyncio.sleep(0.5)
        assert not task.done(), "el alta concurrente debía esperar a la fusión"
        await fusion.commit()

        with pytest.raises(IntegrityError, match="uq_products_tenant_external_code"):
            await asyncio.wait_for(task, timeout=15)

    codes = await _codes(sm, tid)
    assert codes[low] is not None and codes[high] is None
    assert len(codes) == 2
