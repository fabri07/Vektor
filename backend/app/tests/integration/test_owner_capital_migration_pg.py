"""Integración PostgreSQL de la migración ``20261009_0001`` (D·2, capital de socios).

Contra Postgres real porque la idempotencia (comprobar presencia antes de crear)
y los CHECK son lo que se prueba, y SQLite no corre la migración.

Todo pasa dentro de UNA transacción que se revierte al final: el DROP + CREATE
de la tabla es transaccional en Postgres, así que la base compartida queda como
estaba. Correr con ``-n 0``.
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import pathlib
import uuid
from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Connection, insert, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, create_async_engine
from sqlalchemy.inspection import inspect as sa_inspect
from sqlalchemy.pool import NullPool

from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.persistence.models.owner_capital import OwnerCapitalMovement
from app.persistence.models.tenant import Tenant

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]

_TABLA = "owner_capital_movements"


def _load_migration() -> Any:
    ruta = (
        pathlib.Path(__file__).resolve().parents[2]
        / "persistence"
        / "migrations"
        / "versions"
        / "20261009_0001_owner_capital_movements.py"
    )
    spec = importlib.util.spec_from_file_location("_d2_owner_capital", ruta)
    assert spec is not None and spec.loader is not None
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


mig = _load_migration()


def _upgrade(sync_conn: Connection) -> None:
    with Operations.context(MigrationContext.configure(sync_conn)):
        mig.upgrade()


def _tablas(sync_conn: Connection) -> list[str]:
    return sa_inspect(sync_conn).get_table_names()


def _indices(sync_conn: Connection) -> set[str]:
    return {ix["name"] for ix in sa_inspect(sync_conn).get_indexes(_TABLA)}


@contextlib.asynccontextmanager
async def _transaccion() -> AsyncIterator[AsyncConnection]:
    """Conexión con UNA transacción que siempre se revierte.

    Se abre dentro del test (no en un fixture): el fixture corre en otro event
    loop y asyncpg no comparte una conexión entre loops.
    """
    assert TEST_PG_DSN is not None
    engine = create_async_engine(TEST_PG_DSN, poolclass=NullPool)
    try:
        async with engine.connect() as c:
            trans = await c.begin()
            try:
                if _TABLA not in await c.run_sync(_tablas):
                    pytest.skip(f"schema sin migrar (falta {_TABLA}): correr alembic upgrade head")
                yield c
            finally:
                await trans.rollback()
    finally:
        await engine.dispose()


async def _constraints(c: AsyncConnection) -> set[str]:
    filas = await c.execute(
        text(
            "SELECT conname FROM pg_constraint"
            " WHERE conrelid = CAST(:t AS regclass) AND contype = 'c'"
        ),
        {"t": _TABLA},
    )
    return {r[0] for r in filas}


async def test_upgrade_es_idempotente() -> None:
    async with _transaccion() as conn:
        # Sobre la tabla ya migrada (head): no-op.
        await conn.run_sync(_upgrade)
        # Desde cero, dos veces seguidas.
        await conn.execute(text(f"DROP TABLE {_TABLA}"))
        await conn.run_sync(_upgrade)
        await conn.run_sync(_upgrade)

        assert _TABLA in await conn.run_sync(_tablas)
        assert {
            "ix_owner_capital_movements_tenant_id",
            "ix_owner_capital_movements_tenant_date",
        } <= await conn.run_sync(_indices)
        assert await _constraints(conn) == {
            "ck_owner_capital_movements_kind",
            "ck_owner_capital_movements_form",
            "ck_owner_capital_movements_source_type",
            "ck_owner_capital_movements_amount_positive",
            "ck_owner_capital_movements_payment_method",
        }


def _fila(tenant_id: uuid.UUID, **extra: Any) -> dict[str, Any]:
    return {
        "id": uuid.uuid4(),
        "tenant_id": tenant_id,
        "kind": "aporte",
        "form": "dinero",
        "occurred_on": date(2026, 10, 1),
        "payment_method": "cash",
        "amount_ars": Decimal("100.00"),
        "source_type": "manual",
        **extra,
    }


@pytest.mark.parametrize(
    "malo",
    [
        {"amount_ars": Decimal("0")},
        {"amount_ars": Decimal("-1")},
        {"kind": "venta"},
        {"form": "acciones"},
        {"source_type": "inventado"},
        {"payment_method": None},
        {"payment_method": "bitcoin"},
        {"form": "especie"},  # en especie no hay medio de pago
    ],
)
async def test_los_check_rechazan(malo: dict[str, Any]) -> None:
    async with _transaccion() as conn:
        tenant_id = uuid.uuid4()
        s = AsyncSession(bind=conn)
        s.add(Tenant(tenant_id=tenant_id, legal_name="T", display_name="T"))
        await s.flush()

        tabla = OwnerCapitalMovement.__table__
        await conn.execute(insert(tabla).values(**_fila(tenant_id)))
        await conn.execute(
            insert(tabla).values(**_fila(tenant_id, form="especie", payment_method=None))
        )
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await conn.execute(insert(tabla).values(**_fila(tenant_id, **malo)))
