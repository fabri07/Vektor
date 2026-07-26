"""Integración PostgreSQL del `NOT NULL` de `Product.name_normalized` (F8d).

Por qué contra Postgres real
----------------------------
SQLite acepta declarar una columna ``NOT NULL`` pero no la hace cumplir con la
misma fidelidad que Postgres para todos los casos (y, sobre todo, no ejercita
el ``ALTER COLUMN ... SET NOT NULL`` real de la migración). Lo que importa acá
son dos cosas que solo el motor real puede validar:

1. **El fail-safe de la migración.** ``_verify_clean`` tiene que abortar con
   ``RuntimeError`` (no colarse) ante ``NULL`` o ``''``/whitespace — es la razón
   por la que la migración NO usa ``server_default=''`` (ver el docstring de
   ``20260804_0001_product_name_normalized_notnull.py``).
2. **La constraint en la tabla real.** Un ``INSERT`` crudo que saltea el
   listener del ORM tiene que ser rechazado por Postgres con ``IntegrityError``.

Gating: se **skippea limpio** sin ``TEST_PG_DSN``. Para correrlo::

    TEST_PG_DSN='postgresql+asyncpg://vektor:vektor@localhost:5432/vektor' \\
        pytest app/tests/integration/test_product_name_normalized_notnull_pg.py -v --no-cov

Aislamiento bajo xdist: los tests de las funciones de la migración usan TABLAS
TEMPORALES ``products`` (el ``pg_temp`` de la sesión precede a ``public`` en el
``search_path``, así que las queries sin calificar de ``_run_backfill``/
``_verify_clean`` leen la temporal) — no tocan la tabla real ni pisan a otro
worker. El test de la constraint NOT NULL sí usa la tabla ``products`` real
(ya la crea `create_all` con `nullable=False`, que es justo lo que se quiere
probar), pero con un `tenant_id` único que se limpia al final.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import uuid
from collections.abc import AsyncGenerator
from typing import Any, cast

import pytest
import pytest_asyncio
from sqlalchemy import Table, delete, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.domain.text_norm import normalize_product_name
from app.persistence.db.base import Base
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]

_DDL_ADVISORY_KEY = 0x5645_4B54_4F52_0F80  # "VEKTOR" + F8d


def _load_migration() -> Any:
    """Importa la migración por path: ``versions/`` no es un paquete importable."""
    ruta = (
        pathlib.Path(__file__).resolve().parents[2]
        / "persistence"
        / "migrations"
        / "versions"
        / "20260804_0001_product_name_normalized_notnull.py"
    )
    spec = importlib.util.spec_from_file_location("_f8d_migration", ruta)
    assert spec is not None and spec.loader is not None
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


mig = _load_migration()


@pytest_asyncio.fixture(scope="module")
async def pg_engine() -> AsyncGenerator[AsyncEngine, None]:
    assert TEST_PG_DSN is not None
    engine = create_async_engine(TEST_PG_DSN, poolclass=NullPool)
    tables: list[Table] = [
        cast("Table", Tenant.__table__),
        cast("Table", Product.__table__),
    ]
    async with engine.begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _DDL_ADVISORY_KEY})
        # checkfirst: en CI el schema ya lo creó `alembic upgrade head` (que es
        # quien PRUEBA la migración de verdad). Acá queda no-op.
        await conn.run_sync(Base.metadata.create_all, tables=tables, checkfirst=True)
    try:
        yield engine
    finally:
        await engine.dispose()


# ── `_verify_clean` (aborta con datos sucios / pasa con datos limpios) ──────


async def test_verify_clean_aborta_con_name_normalized_null(pg_engine: AsyncEngine) -> None:
    async with pg_engine.connect() as conn:
        await conn.execute(
            text("CREATE TEMP TABLE products (id uuid, name text, name_normalized text)")
        )
        await conn.execute(
            text("INSERT INTO products VALUES (:id, 'Café', NULL)"),
            {"id": uuid.uuid4()},
        )
        with pytest.raises(RuntimeError, match="ABORTADA"):
            await conn.run_sync(lambda c: mig._verify_clean(c))


async def test_verify_clean_aborta_con_name_normalized_vacio(pg_engine: AsyncEngine) -> None:
    async with pg_engine.connect() as conn:
        await conn.execute(
            text("CREATE TEMP TABLE products (id uuid, name text, name_normalized text)")
        )
        await conn.execute(
            text("INSERT INTO products VALUES (:id, 'Café', '')"),
            {"id": uuid.uuid4()},
        )
        with pytest.raises(RuntimeError, match="ABORTADA"):
            await conn.run_sync(lambda c: mig._verify_clean(c))


async def test_verify_clean_aborta_con_name_normalized_solo_whitespace(
    pg_engine: AsyncEngine,
) -> None:
    """El ``trim()`` del `_verify_clean` tiene que atrapar espacios puros, no solo `''`.

    Es justo el caso que justifica por qué la migración NO usa
    ``server_default=''``: un default así dejaría colar exactamente este tipo
    de basura sin que nadie la note.
    """
    async with pg_engine.connect() as conn:
        await conn.execute(
            text("CREATE TEMP TABLE products (id uuid, name text, name_normalized text)")
        )
        await conn.execute(
            text("INSERT INTO products VALUES (:id, 'Café', '   ')"),
            {"id": uuid.uuid4()},
        )
        with pytest.raises(RuntimeError, match="ABORTADA"):
            await conn.run_sync(lambda c: mig._verify_clean(c))


async def test_verify_clean_no_aborta_con_datos_limpios(pg_engine: AsyncEngine) -> None:
    async with pg_engine.connect() as conn:
        await conn.execute(
            text("CREATE TEMP TABLE products (id uuid, name text, name_normalized text)")
        )
        await conn.execute(
            text(
                "INSERT INTO products VALUES "
                "(:i1, 'Café Molido', 'cafe molido'), (:i2, 'Yerba', 'yerba')"
            ),
            {"i1": uuid.uuid4(), "i2": uuid.uuid4()},
        )
        await conn.run_sync(lambda c: mig._verify_clean(c))  # no levanta


# ── `_run_backfill` ──────────────────────────────────────────────────────────


async def test_run_backfill_llena_desde_name(pg_engine: AsyncEngine) -> None:
    async with pg_engine.connect() as conn:
        await conn.execute(
            text("CREATE TEMP TABLE products (id uuid, name text, name_normalized text)")
        )
        pid = uuid.uuid4()
        await conn.execute(
            text("INSERT INTO products VALUES (:id, 'Café Molido', NULL)"),
            {"id": pid},
        )

        await conn.run_sync(lambda c: mig._run_backfill(c))

        fila = (
            await conn.execute(
                text("SELECT name_normalized FROM products WHERE id = :id"), {"id": pid}
            )
        ).scalar_one()
        assert fila == normalize_product_name("Café Molido")

        # Idempotente: una segunda corrida no cambia nada (ya no hay stragglers).
        await conn.run_sync(lambda c: mig._run_backfill(c))
        fila_2 = (
            await conn.execute(
                text("SELECT name_normalized FROM products WHERE id = :id"), {"id": pid}
            )
        ).scalar_one()
        assert fila_2 == fila


async def test_run_backfill_deja_verify_clean_conforme(pg_engine: AsyncEngine) -> None:
    """El flujo real de la migración: backfill seguido de verify_clean sobre lo
    que quedó — sobre un straggler legítimo (name no vacío), no debe abortar."""
    async with pg_engine.connect() as conn:
        await conn.execute(
            text("CREATE TEMP TABLE products (id uuid, name text, name_normalized text)")
        )
        await conn.execute(
            text("INSERT INTO products VALUES (:id, 'Yerba Mate', NULL)"),
            {"id": uuid.uuid4()},
        )
        await conn.run_sync(lambda c: mig._run_backfill(c))
        await conn.run_sync(lambda c: mig._verify_clean(c))  # no levanta


# ── La constraint NOT NULL real (tabla `products` de verdad) ────────────────


@pytest_asyncio.fixture
async def tenant_id(pg_engine: AsyncEngine) -> AsyncGenerator[uuid.UUID, None]:
    tid = uuid.uuid4()
    maker = async_sessionmaker(pg_engine, expire_on_commit=False)
    async with maker() as s:
        s.add(Tenant(tenant_id=tid, legal_name="T", display_name="T"))
        await s.commit()
    try:
        yield tid
    finally:
        async with maker() as s:
            await s.execute(delete(Product).where(Product.tenant_id == tid))
            await s.execute(delete(Tenant).where(Tenant.tenant_id == tid))
            await s.commit()


async def test_constraint_not_null_rechaza_insert_crudo_con_name_normalized_nulo(
    pg_engine: AsyncEngine, tenant_id: uuid.UUID
) -> None:
    """INSERT crudo (SQL directo, saltea el listener del ORM) contra la tabla
    ``products`` REAL — creada por `create_all` con `name_normalized` ya
    `nullable=False` (F8d). Es la prueba de que la semántica del constraint
    rige en Postgres real, algo que SQLite no puede garantizar."""
    inserta = text(
        "INSERT INTO products "
        "(id, tenant_id, name, name_normalized, sale_price_ars, stock_units, "
        " is_active, created_at, updated_at) "
        "VALUES (:id, :t, 'Producto sin normalizar', NULL, 100, 0, true, now(), now())"
    )
    with pytest.raises(IntegrityError):
        async with pg_engine.begin() as conn:
            await conn.execute(inserta, {"id": uuid.uuid4(), "t": tenant_id})


async def test_constraint_not_blank_rechaza_insert_crudo_con_name_normalized_whitespace(
    pg_engine: AsyncEngine, tenant_id: uuid.UUID
) -> None:
    """La CHECK ``ck_products_name_normalized_not_blank`` (defensa en profundidad,
    no redundante con NOT NULL) tiene que rechazar un INSERT crudo con
    ``name_normalized`` de solo espacios — el caso que ``CreateProductRequest``
    ya bloquea a nivel schema, pero un camino que inserte por fuera de ese
    schema (import, script) también tiene que chocar con la DB."""
    inserta = text(
        "INSERT INTO products "
        "(id, tenant_id, name, name_normalized, sale_price_ars, stock_units, "
        " is_active, created_at, updated_at) "
        "VALUES (:id, :t, '   ', '   ', 100, 0, true, now(), now())"
    )
    with pytest.raises(IntegrityError):
        async with pg_engine.begin() as conn:
            await conn.execute(inserta, {"id": uuid.uuid4(), "t": tenant_id})


async def test_constraint_not_null_acepta_insert_crudo_con_valor(
    pg_engine: AsyncEngine, tenant_id: uuid.UUID
) -> None:
    """Control positivo: el mismo INSERT crudo, con `name_normalized` presente,
    tiene que pasar — así el test anterior prueba el constraint y no un typo
    de columna/tipo."""
    pid = uuid.uuid4()
    inserta = text(
        "INSERT INTO products "
        "(id, tenant_id, name, name_normalized, sale_price_ars, stock_units, "
        " is_active, created_at, updated_at) "
        "VALUES (:id, :t, 'Producto ok', 'producto ok', 100, 0, true, now(), now())"
    )
    async with pg_engine.begin() as conn:
        await conn.execute(inserta, {"id": pid, "t": tenant_id})

    async with pg_engine.connect() as conn:
        total = (
            await conn.execute(
                text("SELECT count(*) FROM products WHERE id = :id"), {"id": pid}
            )
        ).scalar_one()
    assert total == 1
