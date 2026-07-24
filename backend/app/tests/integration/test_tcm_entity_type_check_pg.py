"""Integración PostgreSQL up/down de la migración ``20260803_0001`` (F7a).

Por qué contra Postgres real
----------------------------
El CHECK ``ck_tcm_entity_type`` de ``tenant_column_mappings`` es una restricción
de Postgres — SQLite lo IGNORA por completo (acepta cualquier ``entity_type``),
así que la suite SQLite no puede validar que la migración realmente amplía el
dominio, ni que el ``downgrade`` lo restaura. Ver ``[[feedback_sqlite_masks_postgres]]``.

Aislamiento: la migración real apunta a ``tenant_column_mappings``/
``ck_tcm_entity_type`` (constantes de módulo ``_TABLE``/``_CONSTRAINT``). Para no
tocar la tabla real —que otro worker de xdist puede estar usando, y que en este
punto de la cadena YA tiene el CHECK ampliado— cada test repuntea ``mig._TABLE``
(vía ``monkeypatch``) a una tabla scratch con nombre único que solo replica la
columna ``entity_type``. Se ejercita la MISMA lógica (``_recreate``/``upgrade``/
``downgrade``) que corre en producción, sin pisar nada compartido. El nombre del
constraint (``ck_tcm_entity_type``) no necesita aislarse aparte: en Postgres los
nombres de constraint son únicos POR TABLA, no globales.

Gating: se **skippea limpio** sin ``TEST_PG_DSN``. Para correrlo::

    TEST_PG_DSN='postgresql+asyncpg://vektor:vektor@localhost:5432/vektor' \\
        pytest app/tests/integration/test_tcm_entity_type_check_pg.py -v --no-cov
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from alembic.migration import MigrationContext
from alembic.operations import Operations

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]


def _load_migration() -> Any:
    """Importa la migración por path: ``versions/`` no es un paquete importable."""
    ruta = (
        pathlib.Path(__file__).resolve().parents[2]
        / "persistence"
        / "migrations"
        / "versions"
        / "20260803_0001_widen_tcm_entity_type_check.py"
    )
    spec = importlib.util.spec_from_file_location("_f7a_tcm_migration", ruta)
    assert spec is not None and spec.loader is not None
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


mig = _load_migration()


def _run_upgrade(sync_conn: Connection) -> None:
    ctx = MigrationContext.configure(sync_conn)
    with Operations.context(ctx):
        mig.upgrade()


def _run_downgrade(sync_conn: Connection) -> None:
    ctx = MigrationContext.configure(sync_conn)
    with Operations.context(ctx):
        mig.downgrade()


@pytest_asyncio.fixture(scope="module")
async def pg_engine() -> AsyncGenerator[AsyncEngine, None]:
    assert TEST_PG_DSN is not None
    engine = create_async_engine(TEST_PG_DSN, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def scratch_table(pg_engine: AsyncEngine) -> AsyncGenerator[str, None]:
    """Tabla scratch con nombre único: replica solo la columna que el CHECK
    restringe. Nunca se toca ``tenant_column_mappings`` real."""
    nombre = f"f7a_tcm_probe_{uuid.uuid4().hex[:12]}"
    async with pg_engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(text(f"CREATE TABLE {nombre} (entity_type text)"))
    try:
        yield nombre
    finally:
        async with pg_engine.connect() as conn:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            await conn.execute(text(f"DROP TABLE IF EXISTS {nombre} CASCADE"))


async def test_upgrade_acepta_customer_y_supplier_y_rechaza_invalido(
    pg_engine: AsyncEngine,
    scratch_table: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tras ``upgrade``: el dominio ampliado acepta customer/supplier Y sigue
    aceptando los 4 valores legacy (ADDITIVE, no reescribe el dominio) — un
    valor fuera del dominio sigue siendo rechazado."""
    monkeypatch.setattr(mig, "_TABLE", scratch_table)
    async with pg_engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.run_sync(_run_upgrade)

        await conn.execute(
            text(f"INSERT INTO {scratch_table} (entity_type) VALUES ('customer')")
        )
        await conn.execute(
            text(f"INSERT INTO {scratch_table} (entity_type) VALUES ('supplier')")
        )
        for legacy in ("sale", "expense", "product", "inventory"):
            await conn.execute(
                text(f"INSERT INTO {scratch_table} (entity_type) VALUES (:v)"),
                {"v": legacy},
            )

        with pytest.raises(IntegrityError):
            await conn.execute(
                text(f"INSERT INTO {scratch_table} (entity_type) VALUES ('foobar')")
            )


async def test_downgrade_vuelve_a_rechazar_customer_y_supplier(
    pg_engine: AsyncEngine,
    scratch_table: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tras ``downgrade`` de la revisión: customer/supplier vuelven a ser
    rechazados (dominio viejo restaurado) — pero el dominio viejo sigue
    aceptando sus 4 valores originales, sin pisar nada de F5-B/F5-A."""
    monkeypatch.setattr(mig, "_TABLE", scratch_table)
    async with pg_engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.run_sync(_run_upgrade)
        await conn.run_sync(_run_downgrade)

        with pytest.raises(IntegrityError):
            await conn.execute(
                text(f"INSERT INTO {scratch_table} (entity_type) VALUES ('customer')")
            )
        with pytest.raises(IntegrityError):
            await conn.execute(
                text(f"INSERT INTO {scratch_table} (entity_type) VALUES ('supplier')")
            )

        await conn.execute(
            text(f"INSERT INTO {scratch_table} (entity_type) VALUES ('sale')")
        )
