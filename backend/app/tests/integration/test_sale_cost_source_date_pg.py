"""Integración PostgreSQL de M·3-4: migración ``20261017_0002`` (``cost_source_date``).

Contra Postgres real: la migración es idempotente y el downgrade limpia. Todo en
UNA transacción externa que se revierte. Correr con ``-n 0``.
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import pathlib
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.inspection import inspect as sa_inspect
from sqlalchemy.pool import NullPool

from alembic.migration import MigrationContext
from alembic.operations import Operations

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]

_TABLA = "sales_entries"


def _load_migration() -> Any:
    ruta = (
        pathlib.Path(__file__).resolve().parents[2]
        / "persistence" / "migrations" / "versions" / "20261017_0002_sale_cost_source_date.py"
    )
    spec = importlib.util.spec_from_file_location("_m34_sale_cost_source_date", ruta)
    assert spec is not None and spec.loader is not None
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


mig = _load_migration()


def _upgrade(sync_conn: Connection) -> None:
    with Operations.context(MigrationContext.configure(sync_conn)):
        mig.upgrade()


def _downgrade(sync_conn: Connection) -> None:
    with Operations.context(MigrationContext.configure(sync_conn)):
        mig.downgrade()


def _columnas(sync_conn: Connection) -> set[str]:
    return {c["name"] for c in sa_inspect(sync_conn).get_columns(_TABLA)}


@contextlib.asynccontextmanager
async def _transaccion() -> AsyncIterator[AsyncConnection]:
    assert TEST_PG_DSN is not None
    engine = create_async_engine(TEST_PG_DSN, poolclass=NullPool)
    try:
        async with engine.connect() as c:
            trans = await c.begin()
            try:
                if "cost_source_date" not in await c.run_sync(_columnas):
                    pytest.skip("schema sin migrar (falta cost_source_date)")
                yield c
            finally:
                await trans.rollback()
    finally:
        await engine.dispose()


async def test_upgrade_es_idempotente_y_el_downgrade_limpia() -> None:
    async with _transaccion() as conn:
        await conn.run_sync(_upgrade)  # sobre head: no-op
        await conn.run_sync(_downgrade)
        assert "cost_source_date" not in await conn.run_sync(_columnas)
        await conn.run_sync(_upgrade)
        await conn.run_sync(_upgrade)
        assert "cost_source_date" in await conn.run_sync(_columnas)
