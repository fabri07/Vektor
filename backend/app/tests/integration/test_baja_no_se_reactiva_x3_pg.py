"""X3-1 contra PostgreSQL real: ``20261026_0001`` y el backfill de bajas viejas.

Upgrade idempotente, downgrade y vuelta, dentro de una transacción que se
revierte (el DDL de Postgres es transaccional). Con la columna bajada se
siembran usuarios como estaban antes del arreglo y el upgrade marca como baja
sólo los que no pueden ser un registro pendiente de verificar.

Correr con ``-n 0`` contra una base con ``alembic upgrade head``.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
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


def _migracion() -> Any:
    ruta = (
        pathlib.Path(__file__).resolve().parents[2]
        / "persistence"
        / "migrations"
        / "versions"
        / "20261026_0001_users_deactivated_at.py"
    )
    spec = importlib.util.spec_from_file_location("_x3_mig", ruta)
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


def _usuario(conn: Connection, tid: uuid.UUID, nombre: str, *, activo: bool,
             ultimo_login: datetime | None = None) -> uuid.UUID:
    uid = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO users (user_id, tenant_id, email, password_hash, full_name, "
            "role_code, is_active, last_login_at, created_at, updated_at) "
            "VALUES (:u, :t, :e, 'x', :n, 'ADMIN', :a, :l, now(), now())"
        ),
        {"u": uid, "t": tid, "e": f"{nombre}-{uid.hex[:6]}@x3.com", "n": nombre,
         "a": activo, "l": ultimo_login},
    )
    return uid


def _token(conn: Connection, uid: uuid.UUID, *, usado: bool, hace_horas: int) -> None:
    creado = datetime.now(UTC) - timedelta(hours=hace_horas)
    conn.execute(
        text(
            "INSERT INTO email_verification_tokens "
            "(token_id, user_id, expires_at, used, created_at, updated_at) "
            "VALUES (:i, :u, :x, :us, :c, :c)"
        ),
        {"i": uuid.uuid4(), "u": uid, "x": creado + timedelta(hours=24), "us": usado,
         "c": creado},
    )


async def test_migracion_idempotente_reversible_y_backfill(pg_engine: AsyncEngine) -> None:
    def _correr(conn: Connection) -> None:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            mig.upgrade()  # ya aplicada: no-op
            mig.downgrade()
            columnas = {
                r[0]
                for r in conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'users'"
                    )
                )
            }
            assert "deactivated_at" not in columnas

            tid = uuid.uuid4()
            conn.execute(
                text(
                    "INSERT INTO tenants (tenant_id, legal_name, display_name, "
                    "created_at, updated_at) VALUES (:t, 'X3', 'X3', now(), now())"
                ),
                {"t": tid},
            )
            # Bajas: entró alguna vez / el dueño lo creó (sin token) / verificó.
            con_login = _usuario(conn, tid, "login", activo=False,
                                 ultimo_login=datetime.now(UTC))
            sin_token = _usuario(conn, tid, "creado", activo=False)
            verificado = _usuario(conn, tid, "verificado", activo=False)
            _token(conn, verificado, usado=True, hace_horas=48)
            # Indistinguibles de un registro pendiente: quedan como están.
            pendiente = _usuario(conn, tid, "pendiente", activo=False)
            _token(conn, pendiente, usado=False, hace_horas=1)
            reenviado = _usuario(conn, tid, "reenviado", activo=False)
            _token(conn, reenviado, usado=True, hace_horas=48)  # invalidado por el reenvío
            _token(conn, reenviado, usado=False, hace_horas=1)
            activo = _usuario(conn, tid, "activo", activo=True,
                              ultimo_login=datetime.now(UTC))

            mig.upgrade()
            mig.upgrade()  # dos veces: no-op

            marcados = {
                r[0]
                for r in conn.execute(
                    text(
                        "SELECT user_id FROM users "
                        "WHERE tenant_id = :t AND deactivated_at IS NOT NULL"
                    ),
                    {"t": tid},
                )
            }
            assert marcados == {con_login, sin_token, verificado}
            assert pendiente not in marcados
            assert reenviado not in marcados
            assert activo not in marcados

    async with pg_engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.run_sync(_correr)
        finally:
            await trans.rollback()
