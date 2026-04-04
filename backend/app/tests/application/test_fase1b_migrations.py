"""
Tests — Fase 1B migrations.

Los tests de esquema BD conectan a PostgreSQL real vía DATABASE_URL.
Si DATABASE_URL no está disponible, se saltan con un mensaje claro.

Los tests de ConversationService usan SQLite in-memory + Redis mock.
"""

import json
import os
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.persistence.db.base import Base


# ── Helpers de conexión a PostgreSQL real ─────────────────────────────────────

def _get_db_conn():
    """Devuelve conexión psycopg2 a la BD real. Skipea si no hay URL."""
    try:
        import psycopg2
    except ImportError:
        pytest.skip("psycopg2 no disponible")

    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL no definida — test requiere PostgreSQL real")
    url = url.replace("postgresql+asyncpg://", "postgresql://")
    url = url.replace("postgresql+psycopg2://", "postgresql://")
    return psycopg2.connect(url)


# ── Tests de esquema (requieren PostgreSQL real) ──────────────────────────────

def test_business_heuristic_overrides_table_exists():
    """La tabla business_heuristic_overrides debe existir en la BD."""
    conn = _get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT to_regclass('public.business_heuristic_overrides')"
            )
            result = cur.fetchone()[0]
        assert result is not None, "Tabla business_heuristic_overrides no encontrada"
    finally:
        conn.close()


def test_agent_conversation_context_table_exists():
    """La tabla agent_conversation_context debe existir en la BD."""
    conn = _get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT to_regclass('public.agent_conversation_context')"
            )
            result = cur.fetchone()[0]
        assert result is not None, "Tabla agent_conversation_context no encontrada"
    finally:
        conn.close()


def test_businesses_has_heuristics_version_column():
    """business_profiles debe tener la columna heuristics_version."""
    conn = _get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'business_profiles'
                  AND column_name = 'heuristics_version'
                """
            )
            result = cur.fetchone()
        assert result is not None, "Columna heuristics_version no encontrada en business_profiles"
    finally:
        conn.close()


def test_users_has_google_token_columns():
    """La tabla users debe tener las cuatro columnas de Google OAuth."""
    conn = _get_db_conn()
    expected_columns = {
        "google_access_token_encrypted",
        "google_refresh_token_encrypted",
        "google_scopes",
        "google_connected_at",
    }
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'users'
                  AND column_name = ANY(%s)
                """,
                (list(expected_columns),),
            )
            found = {row[0] for row in cur.fetchall()}
        missing = expected_columns - found
        assert not missing, f"Columnas faltantes en users: {missing}"
    finally:
        conn.close()


def test_rls_on_new_tables():
    """RLS debe estar habilitado en las tablas nuevas."""
    conn = _get_db_conn()
    tables = ["business_heuristic_overrides", "agent_conversation_context"]
    try:
        with conn.cursor() as cur:
            for table in tables:
                cur.execute(
                    "SELECT relrowsecurity FROM pg_class WHERE relname = %s",
                    (table,),
                )
                row = cur.fetchone()
                assert row is not None, f"Tabla {table} no encontrada en pg_class"
                assert row[0] is True, f"RLS no habilitado en {table}"
    finally:
        conn.close()


# ── Tests de ConversationService (SQLite in-memory + Redis mock) ──────────────

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def sqlite_session():
    """Sesión SQLite in-memory con todos los modelos creados."""
    import app.persistence.models  # noqa: F401 — registra todos los modelos en Base

    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
def redis_mock():
    """Mock de redis.asyncio con get/setex."""
    mock = MagicMock()
    mock.get = AsyncMock(return_value=None)
    mock.setex = AsyncMock(return_value=True)
    return mock


@pytest.mark.asyncio
async def test_conversation_service_get_context_empty(sqlite_session, redis_mock):
    """get_context devuelve turns vacíos cuando no hay datos en Redis ni PostgreSQL."""
    from app.application.services.conversation_service import ConversationService

    svc = ConversationService(redis_mock, sqlite_session)
    conv_id = str(uuid.uuid4())
    ctx = await svc.get_context(conv_id)

    assert ctx["turns"] == []
    assert ctx["summary"] is None


@pytest.mark.asyncio
async def test_conversation_service_add_turn(sqlite_session, redis_mock):
    """add_turn agrega el turno al contexto y lo cachea en Redis."""
    from app.application.services.conversation_service import ConversationService

    svc = ConversationService(redis_mock, sqlite_session)
    conv_id = str(uuid.uuid4())

    ctx = await svc.add_turn(conv_id, "user", "¿Cuál es mi score?")

    assert len(ctx["turns"]) == 1
    assert ctx["turns"][0]["role"] == "user"
    assert ctx["turns"][0]["content"] == "¿Cuál es mi score?"
    redis_mock.setex.assert_called_once()


@pytest.mark.asyncio
async def test_conversation_service_sliding_window(sqlite_session, redis_mock):
    """Tras 11 turnos, el historial se trunca a los últimos 10."""
    from app.application.services.conversation_service import ConversationService

    # Simular contexto con 10 turnos ya en Redis
    existing_turns = [{"role": "user", "content": f"msg {i}"} for i in range(10)]
    redis_mock.get = AsyncMock(
        return_value=json.dumps({"turns": existing_turns, "summary": None})
    )

    svc = ConversationService(redis_mock, sqlite_session)
    conv_id = str(uuid.uuid4())
    ctx = await svc.add_turn(conv_id, "user", "turno 11")

    assert len(ctx["turns"]) == 10
    assert ctx["turns"][-1]["content"] == "turno 11"


@pytest.mark.asyncio
async def test_conversation_service_redis_cache_hit(sqlite_session, redis_mock):
    """Si Redis tiene el contexto, no se consulta PostgreSQL."""
    from app.application.services.conversation_service import ConversationService

    cached_data = {"turns": [{"role": "assistant", "content": "hola"}], "summary": None}
    redis_mock.get = AsyncMock(return_value=json.dumps(cached_data))

    svc = ConversationService(redis_mock, sqlite_session)
    ctx = await svc.get_context(str(uuid.uuid4()))

    assert ctx["turns"][0]["content"] == "hola"
