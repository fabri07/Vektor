"""Tests unitarios de ``scripts/preflight_product_name_normalized.py`` (F8d).

No usa el schema real vía ``Base.metadata``: desde F8d, ``products.name_normalized``
es NOT NULL a nivel DB (ORM + migración ``20260804_0001``), así que no se puede
sembrar con el modelo actual el estado "sucio" (NULL / vacío) que este script está
pensado para diagnosticar ANTES de que la migración corra. Se arma una tabla
standalone mínima —solo las columnas que el script lee— sin la constraint, para
simular el estado PRE-migración real.

El comportamiento PG-específico de la migración en sí (``_run_backfill``,
``_verify_clean`` abortando, el ``SET NOT NULL`` real) es de la Task 2 (PG);
acá solo se prueban las funciones puras/de conteo del script de diagnóstico.

Se carga el módulo por ruta de archivo (``scripts/`` no es un paquete) — mismo
patrón que ``test_detect_misvoided_purchases.py``. Importar el módulo dispara
``from _db import async_engine_config`` a nivel de módulo, pero ``_db`` solo
define helpers (no conecta), así que el import es seguro sin ``DATABASE_URL``.
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"


def _load_module():
    if str(_SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "preflight_product_name_normalized",
        _SCRIPTS_DIR / "preflight_product_name_normalized.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mod():
    return _load_module()


# ── Tabla standalone (sin la constraint NOT NULL) ───────────────────────────────

_TENANT_A = str(uuid.uuid4())
_TENANT_B = str(uuid.uuid4())


@pytest_asyncio.fixture
async def seeded_session() -> AsyncGenerator[AsyncSession, None]:
    """Products mínima, NULLABLE en ``name_normalized`` (estado pre-F8d), con:

    - fila LIMPIA (no candidata)
    - straggler NULL con name legible (resoluble solo con el backfill)
    - straggler vacío-tras-trim con name legible (idem, resoluble)
    - IRRESOLUBLE: name en sí es whitespace puro (tenant A)
    - IRRESOLUBLE: name en sí son solo guiones (tenant B) — normaliza a ""
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE products ("
                "id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, "
                "name TEXT NOT NULL, name_normalized TEXT"
                ")"
            )
        )
        rows = [
            ("clean", _TENANT_A, "Coca-Cola", "coca cola"),
            ("straggler-null", _TENANT_A, "Sprite", None),
            ("straggler-empty", _TENANT_A, "Fanta", "   "),
            ("irresoluble-whitespace", _TENANT_A, "   ", None),
            ("irresoluble-dashes", _TENANT_B, "---", None),
        ]
        for pid, tenant_id, name, name_normalized in rows:
            await conn.execute(
                text(
                    "INSERT INTO products (id, tenant_id, name, name_normalized) "
                    "VALUES (:id, :tid, :name, :nn)"
                ),
                {"id": pid, "tid": tenant_id, "name": name, "nn": name_normalized},
            )

    async with AsyncSession(engine) as session:
        yield session
    await engine.dispose()


async def test_count_null_counts_only_null_rows(mod, seeded_session: AsyncSession) -> None:
    # 3 filas con name_normalized IS NULL: straggler-null + las 2 irresolubles.
    assert await mod.count_null(seeded_session) == 3


async def test_count_empty_counts_only_non_null_blank_rows(
    mod, seeded_session: AsyncSession
) -> None:
    # Solo straggler-empty: no-NULL pero trim() == ''.
    assert await mod.count_empty(seeded_session) == 1


async def test_find_candidates_excludes_clean_row(mod, seeded_session: AsyncSession) -> None:
    candidates = await mod.find_candidates(seeded_session)
    ids = {row["id"] for row in candidates}
    assert ids == {
        "straggler-null",
        "straggler-empty",
        "irresoluble-whitespace",
        "irresoluble-dashes",
    }
    assert "clean" not in ids


async def test_find_irresolubles_excludes_legible_stragglers(
    mod, seeded_session: AsyncSession
) -> None:
    candidates = await mod.find_candidates(seeded_session)
    irresolubles = mod.find_irresolubles(candidates)
    ids = {row["id"] for row in irresolubles}

    # Los stragglers con name LEGIBLE ("Sprite", "Fanta") no son irresolubles: el
    # backfill de la migración los resuelve solos, no requieren reparación manual.
    assert ids == {"irresoluble-whitespace", "irresoluble-dashes"}


async def test_group_by_tenant_breaks_down_irresolubles_per_tenant(
    mod, seeded_session: AsyncSession
) -> None:
    candidates = await mod.find_candidates(seeded_session)
    irresolubles = mod.find_irresolubles(candidates)
    by_tenant = mod.group_by_tenant(irresolubles)

    assert by_tenant[_TENANT_A] == 1  # irresoluble-whitespace
    assert by_tenant[_TENANT_B] == 1  # irresoluble-dashes


async def test_no_candidates_means_all_resoluble_by_backfill(mod) -> None:
    """Sanity check: sin ninguna fila sucia, todo vacío."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE products ("
                "id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, "
                "name TEXT NOT NULL, name_normalized TEXT NOT NULL"
                ")"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO products (id, tenant_id, name, name_normalized) "
                "VALUES ('x', :tid, 'Agua', 'agua')"
            ),
            {"tid": _TENANT_A},
        )

    async with AsyncSession(engine) as session:
        assert await mod.count_null(session) == 0
        assert await mod.count_empty(session) == 0
        candidates = await mod.find_candidates(session)
        assert candidates == []
        assert mod.find_irresolubles(candidates) == []
    await engine.dispose()
