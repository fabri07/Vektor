"""
Pytest fixtures for Véktor backend tests.

Structure mirrors app/ directory.
"""

import io
import time
import unittest.mock
import uuid
from collections.abc import AsyncGenerator, Generator

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

# PIN por defecto de los usuarios de prueba (los fixtures abren la ventana).
TEST_PIN = "1234"


def _pin_window_key(tenant_id: uuid.UUID, user_id: uuid.UUID) -> str:
    # Delegamos en PinService para que el formato de la key no derive del de prod.
    from app.application.services.pin_service import PinService  # noqa: PLC0415

    return PinService._window_key(tenant_id, user_id)


class FakeRedis:
    """Stub async de Redis con TTL real — cubre lo que usa PinService / rate-limit."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[str | None, float | None]] = {}

    def _is_expired(self, exp: float | None) -> bool:
        return exp is not None and time.monotonic() > exp

    async def get(self, key: str) -> str | None:
        value, exp = self._store.get(key, (None, None))
        if self._is_expired(exp):
            self._store.pop(key, None)
            return None
        return value

    async def exists(self, key: str) -> int:
        return 1 if (await self.get(key)) is not None else 0

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None) -> bool:
        if nx and (await self.get(key)) is not None:
            return False
        exp_time = time.monotonic() + ex if ex is not None else None
        self._store[key] = (value, exp_time)
        return True

    async def delete(self, *keys: str) -> int:
        deleted = 0
        for key in keys:
            if (await self.get(key)) is not None:
                deleted += 1
            self._store.pop(key, None)
        return deleted

    async def incr(self, key: str) -> int:
        val, exp = self._store.get(key, ("0", None))
        if self._is_expired(exp):
            val, exp = "0", None
        new_val = int(val or 0) + 1
        self._store[key] = (str(new_val), exp)
        return new_val

    async def expire(self, key: str, ttl: int) -> bool:
        entry = self._store.get(key)
        if entry is None:
            return False
        self._store[key] = (entry[0], time.monotonic() + ttl)
        return True

    async def aclose(self) -> None:
        pass


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()

# ── Test database (SQLite in-memory for speed) ────────────────────────────────
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


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
        pin_hash=hash_password(TEST_PIN),
    )
    db_session.add(user)
    await db_session.commit()
    return user


@pytest_asyncio.fixture
async def auth_headers(
    sample_user: User, sample_tenant: Tenant, fake_redis: FakeRedis
) -> dict[str, str]:
    # Abrir la ventana de PIN por defecto: la mayoría de los tests de mutación
    # asumen al OWNER ya "desbloqueado". Los tests del gate la borran a propósito.
    await fake_redis.set(
        _pin_window_key(sample_tenant.tenant_id, sample_user.user_id), "1", ex=600
    )
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
async def second_auth_headers(
    db_session: AsyncSession, second_tenant: Tenant, fake_redis: FakeRedis
) -> dict[str, str]:
    user = User(
        user_id=uuid.uuid4(),
        tenant_id=second_tenant.tenant_id,
        email="owner@limpieza.com",
        full_name="Ana García",
        password_hash=hash_password("Secure456"),
        role_code="OWNER",
        is_active=True,
        pin_hash=hash_password(TEST_PIN),
    )
    db_session.add(user)
    await db_session.commit()
    await fake_redis.set(
        _pin_window_key(second_tenant.tenant_id, user.user_id), "1", ex=600
    )
    token = create_access_token(
        {
            "sub": str(user.user_id),
            "tenant_id": str(second_tenant.tenant_id),
            "role_code": "OWNER",
        }
    )
    return {"Authorization": f"Bearer {token}"}


# ── Viewer user (for RBAC tests) ─────────────────────────────────────────────


@pytest_asyncio.fixture
async def viewer_headers(db_session: AsyncSession, sample_tenant: Tenant) -> dict[str, str]:
    user = User(
        user_id=uuid.uuid4(),
        tenant_id=sample_tenant.tenant_id,
        email="viewer@kiosco.com",
        full_name="María Viewer",
        password_hash=hash_password("Secure789"),
        role_code="VIEWER",
        is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    token = create_access_token(
        {
            "sub": str(user.user_id),
            "tenant_id": str(sample_tenant.tenant_id),
            "role_code": "VIEWER",
        }
    )
    return {"Authorization": f"Bearer {token}"}


# ── Celery eager mode (sync execution for job tests) ─────────────────────────


@pytest.fixture(autouse=False)
def celery_eager(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Ejecuta tasks de Celery síncronamente. Usar con @pytest.mark.usefixtures('celery_eager')."""
    from app.jobs.celery_app import celery_app  # noqa: PLC0415

    celery_app.conf.update(task_always_eager=True, task_eager_propagates=True)
    yield
    celery_app.conf.update(task_always_eager=False, task_eager_propagates=False)


# ── Celery mock (prevents Redis connection in tests) ─────────────────────────


@pytest.fixture
def mock_score_trigger():
    from app.application.services.score_trigger_service import trigger_score_recalculation

    with unittest.mock.patch.object(trigger_score_recalculation, "delay") as mock:
        yield mock


# ── HTTP test client ──────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def client(
    db_session: AsyncSession, fake_redis: FakeRedis
) -> AsyncGenerator[AsyncClient, None]:
    from app.main import limiter  # noqa: PLC0415
    from app.persistence.db.redis_client import get_redis  # noqa: PLC0415

    limiter._storage.reset()

    app = create_app()

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    async def override_redis() -> AsyncGenerator[FakeRedis, None]:
        yield fake_redis

    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_redis] = override_redis

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac


# ── Reusable file fixtures ───────────────────────────────────────────────────


@pytest.fixture
def xlsx_bytes() -> bytes:
    openpyxl = pytest.importorskip("openpyxl")

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["fecha", "monto", "descripcion"])
    sheet.append(["2024-01-15", "50000", "Venta del día"])
    sheet.append(["2024-01-16", "35000", "Venta tarde"])
    buf = io.BytesIO()
    workbook.save(buf)
    return buf.getvalue()


@pytest.fixture
def csv_bytes() -> bytes:
    return (
        b"fecha,monto,descripcion\n"
        b"2024-01-15,50000,Venta del dia\n"
        b"2024-01-16,35000,Venta tarde\n"
    )


@pytest.fixture
def txt_bytes() -> bytes:
    return b"Venta del dia $50.000\nGasto proveedor $12.000\nStock mercaderia $8.000\n"


@pytest.fixture
def docx_bytes() -> bytes:
    docx = pytest.importorskip("docx")

    document = docx.Document()
    document.add_paragraph("Venta del dia $50.000")
    document.add_paragraph("Gasto proveedor $12.000")
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


@pytest.fixture
def pptx_bytes() -> bytes:
    pptx = pytest.importorskip("pptx")

    presentation = pptx.Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[1])
    slide.shapes.title.text = "Resumen financiero"
    slide.placeholders[1].text = "Venta del dia $50.000\nGasto proveedor $12.000"
    buf = io.BytesIO()
    presentation.save(buf)
    return buf.getvalue()


@pytest.fixture
def pdf_bytes() -> bytes:
    pypdf = pytest.importorskip("pypdf")

    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=300, height=300)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


@pytest.fixture
def png_bytes() -> bytes:
    image_module = pytest.importorskip("PIL.Image")

    image = image_module.new("RGB", (1, 1), color=(255, 0, 0))
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def jpg_bytes() -> bytes:
    image_module = pytest.importorskip("PIL.Image")

    image = image_module.new("RGB", (1, 1), color=(255, 255, 255))
    buf = io.BytesIO()
    image.save(buf, format="JPEG")
    return buf.getvalue()
