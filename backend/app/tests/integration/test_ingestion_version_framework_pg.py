"""Integración PostgreSQL up/down de la migración ``20260805_0001`` (F9a).

Por qué contra Postgres real
----------------------------
La migración contiene SQL dialect-specific:

1. El `UPDATE ... WHERE parsed_summary_json ? 'column_risk_decisions'` usa el
   operador JSONB `?` de PostgreSQL — SQLite no lo entiende ni lo parsea.
   Con SQLite, ese UPDATE se ejecutaría como un NOOP (la condición sería siempre
   falsa, sin error). Ver ``[[feedback_sqlite_masks_postgres]]``.

2. La columna `reread_summary` debe ser `jsonb` en Postgres (no `json`), para
   soportar operadores jsonb-only en el futuro. SQLite ignora la diferencia.

Un test con SQLite pasaría aunque la migración genere el tipo equivocado en Neon.

Aislamiento: como en test_tcm_entity_type_check_pg.py, monkeypatcheamos el nombre
de la tabla a una tabla scratch única (``scratch_table``, sin FKs — ver su
definición más abajo), evitando pisar la tabla real ``uploaded_files`` que el CI
ya migró vía ``alembic upgrade head``. La tabla scratch se crea SIN foreign keys
(``tenant_id`` es una columna UUID cualquiera), así que no hace falta sembrar un
``Tenant`` real para respetar ninguna FK.

Gating: se **skippea limpio** sin ``TEST_PG_DSN``. Para correrlo::

    TEST_PG_DSN='postgresql+asyncpg://vektor:vektor@localhost:5432/vektor' \\
        pytest app/tests/integration/test_ingestion_version_framework_pg.py -v --no-cov
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import Connection, text
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
        / "20260805_0001_ingestion_version_framework.py"
    )
    spec = importlib.util.spec_from_file_location("_f9a_ingestion_version_mig", ruta)
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
    """Tabla scratch con nombre único que replica la estructura de uploaded_files.

    Evita pisar la tabla real, que el CI ya migró vía ``alembic upgrade head``.
    Aislamiento bajo xdist: cada test usa su propia tabla temporal.
    """
    nombre = f"f9a_ingestion_probe_{uuid.uuid4().hex[:12]}"
    async with pg_engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        # Crear tabla con columnas suficientes para el test (sin FKs).
        await conn.execute(
            text(f"""
                CREATE TABLE {nombre} (
                    id UUID NOT NULL PRIMARY KEY,
                    tenant_id UUID NOT NULL,
                    original_filename TEXT NOT NULL,
                    s3_key TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    size_bytes INT NOT NULL,
                    purpose TEXT NOT NULL,
                    status TEXT,
                    processing_status TEXT NOT NULL,
                    parsed_summary_json JSONB,
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
                    updated_at TIMESTAMP WITH TIME ZONE NOT NULL
                )
            """)
        )
    try:
        yield nombre
    finally:
        async with pg_engine.connect() as conn:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            await conn.execute(text(f"DROP TABLE IF EXISTS {nombre} CASCADE"))


async def test_upgrade_agrega_columnas_con_tipos_correctos(
    pg_engine: AsyncEngine,
    scratch_table: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tras ``upgrade`` sobre tabla scratch: las 5 columnas se agregan con tipos correctos.

    Verificamos especialmente que ``reread_summary`` es JSONB (no JSON) y que
    el CHECK de ``reread_status`` está en lugar.
    """
    monkeypatch.setattr(mig, "TABLE_NAME", scratch_table)
    async with pg_engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.run_sync(_run_upgrade)

        # Verificar que las columnas existen con los tipos correctos.
        result = await conn.execute(
            text(f"""
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_name = '{scratch_table}'
            AND column_name IN ('ingestion_version', 'latest_preview_version',
                                'reread_status', 'reread_at', 'reread_summary')
            ORDER BY column_name
            """)
        )
        columns = {row[0]: row[1] for row in result}

        assert "ingestion_version" in columns
        assert "latest_preview_version" in columns
        assert "reread_status" in columns
        assert "reread_at" in columns
        assert "reread_summary" in columns

        # **Verificación crítica:** reread_summary debe ser jsonb, no json
        assert columns["reread_summary"] == "jsonb", (
            f"reread_summary debe ser jsonb, pero es {columns['reread_summary']}"
        )

        # Verificar que el CHECK existe
        constraint_result = await conn.execute(
            text(f"""
            SELECT constraint_name
            FROM information_schema.table_constraints
            WHERE table_name = '{scratch_table}'
            AND constraint_name = 'ck_uploaded_files_reread_status'
            AND constraint_type = 'CHECK'
            """)
        )
        constraints = list(constraint_result)
        assert len(constraints) == 1, "CHECK constraint no encontrado"


async def test_upgrade_marca_archivos_confirmados_con_column_risk_decisions(
    pg_engine: AsyncEngine,
    scratch_table: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tras ``upgrade`` sobre tabla scratch: archivos con ``column_risk_decisions``
    se marcan con ``ingestion_version=2``. Los demás quedan en 1.
    """
    monkeypatch.setattr(mig, "TABLE_NAME", scratch_table)
    tenant_id = uuid.uuid4()

    async with pg_engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")

        # Insertar filas de prueba ANTES de upgrade
        file_with_risk_key = uuid.uuid4()
        file_without_risk_key = uuid.uuid4()

        await conn.execute(
            text(f"""
            INSERT INTO {scratch_table}
            (id, tenant_id, original_filename, s3_key, content_type, size_bytes,
             purpose, status, processing_status, parsed_summary_json, created_at, updated_at)
            VALUES (:id, :tenant_id, :name, :s3_key, :ct, :size, :purpose, :status,
                    :ps, :summary, :created, :updated)
            """),
            {
                "id": file_with_risk_key,
                "tenant_id": tenant_id,
                "name": "test_with_risk.xlsx",
                "s3_key": f"test-{file_with_risk_key}.xlsx",
                "ct": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "size": 1000,
                "purpose": "ingestion",
                "status": "processed",
                "ps": "DONE",
                "summary": json.dumps({
                    "row_count": 100,
                    "column_risk_decisions": {  # Clave que gatilla F8+
                        "column_1": {"drop_column": True}
                    }
                }),
                "created": datetime.now(UTC),
                "updated": datetime.now(UTC),
            },
        )

        await conn.execute(
            text(f"""
            INSERT INTO {scratch_table}
            (id, tenant_id, original_filename, s3_key, content_type, size_bytes,
             purpose, status, processing_status, parsed_summary_json, created_at, updated_at)
            VALUES (:id, :tenant_id, :name, :s3_key, :ct, :size, :purpose, :status,
                    :ps, :summary, :created, :updated)
            """),
            {
                "id": file_without_risk_key,
                "tenant_id": tenant_id,
                "name": "test_without_risk.xlsx",
                "s3_key": f"test-{file_without_risk_key}.xlsx",
                "ct": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "size": 1000,
                "purpose": "ingestion",
                "status": "processed",
                "ps": "DONE",
                "summary": json.dumps({
                    "row_count": 50,
                    "columns": ["col1", "col2"]
                    # Sin column_risk_decisions
                }),
                "created": datetime.now(UTC),
                "updated": datetime.now(UTC),
            },
        )

        # Ahora correr upgrade
        await conn.run_sync(_run_upgrade)

        # Verificar que los valores de ingestion_version se asignaron correctamente
        result_with_risk = await conn.execute(
            text(f"""
            SELECT ingestion_version FROM {scratch_table} WHERE id = :id
            """),
            {"id": file_with_risk_key},
        )
        version_with_risk = result_with_risk.scalar()
        assert version_with_risk == 2, (
            f"Archivo con column_risk_decisions debe tener ingestion_version=2, "
            f"pero tiene {version_with_risk}"
        )

        result_without_risk = await conn.execute(
            text(f"""
            SELECT ingestion_version FROM {scratch_table} WHERE id = :id
            """),
            {"id": file_without_risk_key},
        )
        version_without_risk = result_without_risk.scalar()
        assert version_without_risk == 1, (
            f"Archivo sin column_risk_decisions debe tener ingestion_version=1, "
            f"pero tiene {version_without_risk}"
        )


async def test_downgrade_elimina_columnas(
    pg_engine: AsyncEngine,
    scratch_table: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tras ``downgrade`` sobre tabla scratch: las 5 columnas se eliminan y el CHECK desaparece."""
    monkeypatch.setattr(mig, "TABLE_NAME", scratch_table)
    async with pg_engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")

        # Upgrade primero
        await conn.run_sync(_run_upgrade)

        # Verificar que existen
        result = await conn.execute(
            text(f"""
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_name = '{scratch_table}'
            AND column_name IN ('ingestion_version', 'latest_preview_version',
                                'reread_status', 'reread_at', 'reread_summary')
            """)
        )
        count_before = result.scalar()
        assert count_before == 5, f"Esperaba 5 columnas, encontré {count_before}"

        # Downgrade
        await conn.run_sync(_run_downgrade)

        # Verificar que desaparecieron
        result = await conn.execute(
            text(f"""
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_name = '{scratch_table}'
            AND column_name IN ('ingestion_version', 'latest_preview_version',
                                'reread_status', 'reread_at', 'reread_summary')
            """)
        )
        count_after = result.scalar()
        assert count_after == 0, f"Esperaba 0 columnas tras downgrade, encontré {count_after}"

        # Verificar que el CHECK desapareció
        constraint_result = await conn.execute(
            text(f"""
            SELECT COUNT(*) FROM information_schema.table_constraints
            WHERE table_name = '{scratch_table}'
            AND constraint_name = 'ck_uploaded_files_reread_status'
            """)
        )
        constraint_count = constraint_result.scalar()
        assert constraint_count == 0, "CHECK constraint debería haber sido eliminado"
