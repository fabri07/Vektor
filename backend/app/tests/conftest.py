"""
Pytest fixtures for Véktor backend tests.

Structure mirrors app/ directory.
"""

import asyncio
import unittest.mock
import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config.settings import get_settings
from app.main import create_app
from app.persistence.db.base import Base
from app.persistence.db.session import get_db_session
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.utils.security import create_access_token, hash_password

settings = get_settings()

# ── Test database (SQLite in-memory for speed) ────────────────────────────────
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="function")
async def db_engine():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session


# ── Sample entities ───────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def sample_tenant(db_session: AsyncSession) -> Tenant:
    tenant = Tenant(
        tenant_id=uuid.uuid4(),
        legal_name="Kiosco El Rápido",
        display_name="Kiosco El Rápido",
        currency="ARS",
        pricing_reference_mode="MEP",
        status="ACTIVE",
    )
    db_session.add(tenant)
    await db_session.commit()
    return tenant


@pytest_asyncio.fixture
async def sample_user(db_session: AsyncSession, sample_tenant: Tenant) -> User:
    user = User(
        user_id=uuid.uuid4(),
        tenant_id=sample_tenant.tenant_id,
        email="owner@kiosco.com",
        full_name="Juan Pérez",
        password_hash=hash_password("Secure123"),
        role_code="OWNER",
        is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    return user


@pytest_asyncio.fixture
async def auth_headers(sample_user: User, sample_tenant: Tenant) -> dict[str, str]:
    token = create_access_token(
        {
            "sub": str(sample_user.user_id),
            "tenant_id": str(sample_tenant.tenant_id),
            "role_code": "OWNER",
        }
    )
    return {"Authorization": f"Bearer {token}"}


# ── Second tenant (for isolation tests) ──────────────────────────────────────

@pytest_asyncio.fixture
async def second_tenant(db_session: AsyncSession) -> Tenant:
    tenant = Tenant(
        tenant_id=uuid.uuid4(),
        legal_name="Limpieza Brillante",
        display_name="Limpieza Brillante",
        currency="ARS",
        pricing_reference_mode="MEP",
        status="ACTIVE",
    )
    db_session.add(tenant)
    await db_session.commit()
    return tenant


@pytest_asyncio.fixture
async def second_auth_headers(db_session: AsyncSession, second_tenant: Tenant) -> dict[str, str]:
    user = User(
        user_id=uuid.uuid4(),
        tenant_id=second_tenant.tenant_id,
        email="owner@limpieza.com",
        full_name="Ana García",
        password_hash=hash_password("Secure456"),
        role_code="OWNER",
        is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    token = create_access_token(
        {
            "sub": str(user.user_id),
            "tenant_id": str(second_tenant.tenant_id),
            "role_code": "OWNER",
        }
    )
    return {"Authorization": f"Bearer {token}"}


# ── Celery mock (prevents Redis connection in tests) ─────────────────────────

@pytest.fixture
def mock_score_trigger():
    from app.application.services.score_trigger_service import trigger_score_recalculation

    with unittest.mock.patch.object(trigger_score_recalculation, "delay") as mock:
        yield mock


# ── HTTP test client ──────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    app = create_app()

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_db_session] = override_session

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac
