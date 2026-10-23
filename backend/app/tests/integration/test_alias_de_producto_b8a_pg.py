"""B·8a contra PostgreSQL real: la migración y la carrera del alias.

- ``20261021_0004``: upgrade idempotente, downgrade y vuelta, dentro de una
  transacción que se revierte (el DDL de Postgres es transaccional), y el único
  ``(tenant_id, alias_normalizado)`` rechaza un duplicado.
- Dos ``link-product`` a la vez con el mismo texto del archivo para productos
  DISTINTOS: los dos validan que el alias está libre, el segundo choca con el
  índice único al insertar y eso es un 409 ``ALIAS_OTRO_PRODUCTO``, nunca un 500.
  En SQLite no hay espera por el índice y la suite común no lo puede probar.

Correr con ``-n 0`` contra una base con ``alembic upgrade head``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import pathlib
import uuid
from collections.abc import AsyncGenerator
from typing import Any, cast

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import Connection, delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.api.v1 import others
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.inventory import InventoryBalance, InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.product_alias import ProductAlias
from app.persistence.models.product_cost import ProductCostHistory, ProductCostState
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.models.unclassified_record import UnclassifiedRecord
from app.persistence.models.user import User
from app.tests._respaldo import producto_con_compra

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]


def _migracion() -> Any:
    ruta = (
        pathlib.Path(__file__).resolve().parents[2]
        / "persistence"
        / "migrations"
        / "versions"
        / "20261021_0004_product_aliases.py"
    )
    spec = importlib.util.spec_from_file_location("_b8a_mig", ruta)
    assert spec is not None and spec.loader is not None
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


mig = _migracion()


@pytest_asyncio.fixture(scope="module")
async def pg_engine() -> AsyncGenerator[AsyncEngine, None]:
    assert TEST_PG_DSN is not None
    engine = create_async_engine(TEST_PG_DSN, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


async def test_la_migracion_es_idempotente_y_reversible(pg_engine: AsyncEngine) -> None:
    def _correr(sync_conn: Connection) -> None:
        ctx = MigrationContext.configure(sync_conn)
        with Operations.context(ctx):
            mig.upgrade()  # ya aplicada: no-op
            mig.downgrade()
            assert not sync_conn.execute(
                text("SELECT to_regclass('product_aliases')")
            ).scalar()
            mig.upgrade()
            mig.upgrade()  # dos veces: no-op

    async with pg_engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.run_sync(_correr)
            tid, pid = uuid.uuid4(), uuid.uuid4()
            s = AsyncSession(bind=conn, join_transaction_mode="create_savepoint")
            s.add(Tenant(tenant_id=tid, legal_name="B8a", display_name="B8a"))
            await s.flush()
            s.add(Product(id=pid, tenant_id=tid, name="P", sale_price_ars=10, stock_units=0))
            await s.flush()
            insert = text(
                "INSERT INTO product_aliases (id, tenant_id, product_id, alias, alias_normalizado) "
                "VALUES (:i, :t, :p, 'Coca', 'coca')"
            )
            await conn.execute(insert, {"i": uuid.uuid4(), "t": tid, "p": pid})
            with pytest.raises(IntegrityError):
                async with conn.begin_nested():
                    await conn.execute(insert, {"i": uuid.uuid4(), "t": tid, "p": pid})
        finally:
            await trans.rollback()


_Escenario = tuple[
    async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID, list[uuid.UUID], list[uuid.UUID]
]


@pytest_asyncio.fixture
async def escenario(pg_engine: AsyncEngine) -> AsyncGenerator[_Escenario, None]:
    fabrica = async_sessionmaker(pg_engine, expire_on_commit=False)
    tid, uid = uuid.uuid4(), uuid.uuid4()
    async with fabrica() as s:
        s.add(Tenant(tenant_id=tid, legal_name="B8a", display_name="B8a"))
        await s.flush()
        s.add(
            User(
                user_id=uid, tenant_id=tid, email=f"{uid}@b8a.test", full_name="Dueña",
                password_hash="x", role_code="OWNER", is_active=True,
            )
        )
        await s.commit()
        productos = [
            (await producto_con_compra(s, tid, nombre=nombre)).id
            for nombre in ("Coca Cola 500ml", "Coca Zero 500ml")
        ]
        filas = []
        for _ in range(2):
            rec = UnclassifiedRecord(
                tenant_id=tid, source="ingestion", context_label="Venta sin producto",
                row_data={"fecha": "2024-03-15", "monto": "3000", "cantidad": "1",
                          "medio_de_pago": "cash", "producto": "Coca 500"},
                suggested_entity="sale", status="PENDING",
            )
            s.add(rec)
            await s.flush()
            filas.append(rec.id)
        await s.commit()
    try:
        yield fabrica, tid, uid, productos, filas
    finally:
        async with fabrica() as s:
            for modelo in (
                DecisionAuditLog, ProductAlias, SaleEntry, ProductCostHistory, ProductCostState,
                InventoryMovement, InventoryBalance, UnclassifiedRecord, Product, User,
            ):
                await s.execute(delete(modelo).where(modelo.tenant_id == tid))
            await s.execute(delete(Tenant).where(Tenant.tenant_id == tid))
            await s.commit()


async def test_dos_vinculos_simultaneos_del_mismo_alias_dan_un_409_no_un_500(
    escenario: _Escenario,
) -> None:
    fabrica, tid, uid, productos, filas = escenario

    async def vincular(s: AsyncSession, fila: uuid.UUID, producto: uuid.UUID) -> str:
        tenant = await s.get(Tenant, tid)
        user = await s.get(User, uid)
        assert tenant is not None and user is not None
        try:
            await others.link_product(
                fila,
                others.LinkProductRequest(product_id=producto),
                tenant=tenant,
                user=user,
                _maintenance_guard=None,
                session=s,
            )
        except HTTPException as exc:
            await s.rollback()  # lo que hace `get_db_session` ante el error
            detalle = cast("dict[str, Any]", exc.detail)
            return f"{exc.status_code}:{detalle['code']}"
        return "200"

    async with fabrica() as s1, fabrica() as s2:
        primero = await vincular(s1, filas[0], productos[0])  # alias insertado, sin commit
        segunda = asyncio.create_task(vincular(s2, filas[1], productos[1]))
        await asyncio.sleep(0.5)
        assert not segunda.done(), "la segunda tenía que esperar el índice único"
        await s1.commit()
        resultado = await segunda
        if resultado == "200":
            await s2.commit()

    assert (primero, resultado) == ("200", "409:ALIAS_OTRO_PRODUCTO")
    async with fabrica() as s:
        alias = (
            await s.execute(select(ProductAlias).where(ProductAlias.tenant_id == tid))
        ).scalars().all()
        ventas = await s.scalar(
            select(func.count(SaleEntry.id)).where(SaleEntry.tenant_id == tid)
        )
        pendiente = await s.get(UnclassifiedRecord, filas[1])
    assert [a.product_id for a in alias] == [productos[0]]
    assert ventas == 1
    assert pendiente is not None and pendiente.status == "PENDING"
