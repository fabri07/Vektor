"""Integración PostgreSQL de M·3-3: migración ``20261017_0001`` (``tax_kind``).

Contra Postgres real: idempotencia, CHECK ``NOT VALID`` → ``VALIDATE`` y que el
CHECK rechace un tipo fuera del catálogo. Todo en UNA transacción externa que se
revierte. Correr con ``-n 0``.
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import pathlib
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.inspection import inspect as sa_inspect
from sqlalchemy.pool import NullPool

from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]

_TABLA = "expense_entries"
_CHECK = "ck_expense_entries_tax_kind"


def _load_migration() -> Any:
    ruta = (
        pathlib.Path(__file__).resolve().parents[2]
        / "persistence" / "migrations" / "versions" / "20261017_0001_expense_tax_kind.py"
    )
    spec = importlib.util.spec_from_file_location("_m33_expense_tax_kind", ruta)
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
                if "tax_kind" not in await c.run_sync(_columnas):
                    pytest.skip("schema sin migrar (falta tax_kind)")
                yield c
            finally:
                await trans.rollback()
    finally:
        await engine.dispose()


async def _check_validado(c: AsyncConnection) -> bool | None:
    fila = (
        await c.execute(
            text(
                "SELECT convalidated FROM pg_constraint"
                " WHERE conrelid = CAST(:t AS regclass) AND conname = :n"
            ),
            {"t": _TABLA, "n": _CHECK},
        )
    ).first()
    return None if fila is None else bool(fila[0])


async def test_upgrade_es_idempotente_y_el_downgrade_limpia() -> None:
    async with _transaccion() as conn:
        await conn.run_sync(_upgrade)  # sobre head: no-op
        await conn.run_sync(_downgrade)
        assert "tax_kind" not in await conn.run_sync(_columnas)
        assert await _check_validado(conn) is None

        await conn.run_sync(_upgrade)
        await conn.run_sync(_upgrade)
        assert "tax_kind" in await conn.run_sync(_columnas)
        assert await _check_validado(conn) is True


async def test_el_check_rechaza_un_tipo_fuera_del_catalogo() -> None:
    from sqlalchemy import Table, insert  # noqa: PLC0415
    from sqlalchemy.exc import IntegrityError  # noqa: PLC0415

    async with _transaccion() as conn:
        tenant_id = uuid.uuid4()
        await conn.execute(
            insert(Tenant.__table__).values(  # type: ignore[arg-type]
                tenant_id=tenant_id, legal_name="T", display_name="T"
            )
        )
        tabla: Table = ExpenseEntry.__table__  # type: ignore[assignment]
        fila = {
            "id": uuid.uuid4(), "tenant_id": tenant_id, "amount": Decimal("1"),
            "category": "TAXES", "expense_type": "OPEX", "description": "x",
            "transaction_date": datetime(2026, 10, 1), "is_recurring": False,
            "payment_method": "cash", "provenance": "REAL", "has_user_edits": False,
            "custom_fields": {},
        }
        await conn.execute(insert(tabla).values(**fila))  # NULL = sin tipo
        await conn.execute(insert(tabla).values(**{**fila, "id": uuid.uuid4(), "tax_kind": "iva"}))
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await conn.execute(
                    insert(tabla).values(**{**fila, "id": uuid.uuid4(), "tax_kind": "ganancias"})
                )
