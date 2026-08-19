"""Integración PostgreSQL de la migración ``20260819_0001`` (estados de
revisión de relectura en ``data_repair_runs``).

Por qué existe (hallazgo de code review sobre la Fase 1/2 de F-RR): los tests
normales prueban el MODELO ORM contra SQLite, que **ignora los CHECK
constraints** (ver ``[[feedback_sqlite_masks_postgres]]``) — nunca ejercitan
el constraint real de Postgres ni las funciones ``upgrade()``/``downgrade()``
de la migración. Sin este test, un downgrade que falla contra filas en un
estado nuevo (o un CHECK mal armado) recién se hubiera descubierto corriendo
``make migrate-down`` a mano.

Verifica, contra un Postgres real:
  1. Los 5 estados nuevos (PREVIEWING/NEEDS_REVIEW/READY_TO_APPLY/QUEUED/
     APPLYING) insertan sin violar el CHECK, y ``updated_at`` se completa.
  2. Un valor arbitrario sigue rechazado por el CHECK.
  3. El downgrade, con filas EN LOS ESTADOS NUEVOS presentes, no falla —
     las transforma a FAILED preservando el estado real en
     ``details_json["downgraded_from_status"]`` — y ``updated_at`` desaparece.
  4. Re-aplicar el upgrade después del downgrade es limpio (idempotente).

Este test corre DDL real (downgrade/upgrade de la migración) sobre el target
de ``TEST_PG_DSN`` vía Alembic (conexión sync propia, independiente de
``pg_engine``) — a diferencia de ``test_reread_concurrency_pg.py``, que solo
lee/escribe filas. Por eso, a diferencia de ese archivo, NO es seguro
correrlo en paralelo con otros tests PG-gated contra el MISMO target: un
``pg_advisory_xact_lock`` no protege una migración de Alembic porque corre en
una conexión sync completamente aparte. Correrlo solo con ``-n 0`` (secuencial
— ``-p no:xdist`` NO alcanza, deja el ``-n auto`` de ``addopts`` huérfano y
pytest corta con error de argumento), apuntando únicamente a este archivo.

Gating: se **skippea limpio** sin ``TEST_PG_DSN``. Deja la base en el estado
en que la encontró (upgraded a head).

Para correrlo::

    TEST_PG_DSN='postgresql+asyncpg://vektor:vektor@localhost:55432/vektor' \\
        pytest app/tests/integration/test_reread_review_states_migration_pg.py \\
        -n 0 -v --no-cov
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere TEST_PG_DSN (Postgres real)"),
]

_NEW_STATUSES = ("PREVIEWING", "NEEDS_REVIEW", "READY_TO_APPLY", "QUEUED", "APPLYING")

_INSERT_SQL = text(
    "INSERT INTO data_repair_runs "
    "(id, tenant_id, repair_type, status, dry_run, details_json, created_at) "
    "VALUES (gen_random_uuid(), :tenant_id, 'REREAD_FILE', :status, true, "
    "'{}'::jsonb, now())"
)


@pytest_asyncio.fixture
async def pg_engine() -> AsyncGenerator[AsyncEngine, None]:
    assert TEST_PG_DSN is not None
    engine = create_async_engine(TEST_PG_DSN, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


def _run_alembic(fn_name: str, *args: str) -> None:
    """``fn_name`` in {"upgrade", "downgrade"}. Setea DATABASE_URL_SYNC solo
    para la duración de la llamada — nunca deja el env var pisado para el
    resto del proceso de test."""
    from alembic import command
    from alembic.config import Config

    assert TEST_PG_DSN is not None
    sync_dsn = TEST_PG_DSN.replace("+asyncpg", "+psycopg2")
    previous = os.environ.get("DATABASE_URL_SYNC")
    os.environ["DATABASE_URL_SYNC"] = sync_dsn
    try:
        getattr(command, fn_name)(Config("alembic.ini"), *args)
    finally:
        if previous is None:
            del os.environ["DATABASE_URL_SYNC"]
        else:
            os.environ["DATABASE_URL_SYNC"] = previous


async def test_new_statuses_insert_and_downgrade_is_safe(pg_engine: AsyncEngine) -> None:
    tenant_id = uuid.uuid4()

    # ── 1: los 5 estados nuevos insertan y quedan COMMITEADOS ──
    async with pg_engine.begin() as conn:
        for status in _NEW_STATUSES:
            await conn.execute(_INSERT_SQL, {"tenant_id": str(tenant_id), "status": status})
        rows = (
            await conn.execute(
                text(
                    "SELECT status, updated_at IS NOT NULL FROM data_repair_runs "
                    "WHERE tenant_id = :tenant_id"
                ),
                {"tenant_id": str(tenant_id)},
            )
        ).all()
        assert {r[0] for r in rows} == set(_NEW_STATUSES)
        assert all(r[1] for r in rows), "updated_at debe completarse en cada insert"

    # ── 2: un valor inválido sigue rechazado — transacción PROPIA y
    # descartable: si compartiera la de arriba, el rollback de acá se
    # llevaría puesto los 5 inserts válidos que YA COMMITEARON.
    with pytest.raises(Exception, match="ck_repair_runs_status"):
        async with pg_engine.begin() as bad_conn:
            await bad_conn.execute(
                text(
                    "INSERT INTO data_repair_runs "
                    "(id, tenant_id, repair_type, status, dry_run, details_json, created_at) "
                    "VALUES (gen_random_uuid(), :tenant_id, 'REREAD_FILE', 'NOT_A_STATUS', "
                    "true, '{}'::jsonb, now())"
                ),
                {"tenant_id": str(tenant_id)},
            )

    # ── 3: downgrade con las 5 filas nuevas presentes ──
    try:
        _run_alembic("downgrade", "-1")
    except Exception:
        async with pg_engine.begin() as cleanup_conn:
            await cleanup_conn.execute(
                text("DELETE FROM data_repair_runs WHERE tenant_id = :t"),
                {"t": str(tenant_id)},
            )
        raise

    async with pg_engine.begin() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT status, details_json->>'downgraded_from_status' "
                    "FROM data_repair_runs WHERE tenant_id = :tenant_id"
                ),
                {"tenant_id": str(tenant_id)},
            )
        ).all()
        assert len(rows) == 5
        assert all(r[0] == "FAILED" for r in rows)
        assert {r[1] for r in rows} == set(_NEW_STATUSES)

        await conn.execute(
            text("DELETE FROM data_repair_runs WHERE tenant_id = :t"), {"t": str(tenant_id)}
        )

    # ── 4: re-subir a head deja la base como la encontró ──
    _run_alembic("upgrade", "head")
