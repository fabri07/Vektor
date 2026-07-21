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
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
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

# bcrypt con work factor 4 (mínimo válido) SOLO en tests: los fixtures hashean
# passwords/PINs miles de veces por suite y rounds=12 (~0.3s por hash) era uno
# de los cuellos del tiempo total. La seguridad de prod no cambia (security.py
# sigue en 12); verify() sigue funcionando igual.
from app.utils import security as _security  # noqa: E402

_security._pwd_context.update(bcrypt__rounds=4)

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
#
# Patrón rápido (bajó el suite de ~2h30 a minutos):
#   - `db_engine` es scope="session": el schema (~60 tablas) se crea UNA vez por
#     proceso (por worker de xdist), no una vez por test.
#   - `db_session` abre una transacción EXTERNA sobre la única conexión
#     (StaticPool) y le da al test una AsyncSession con
#     `join_transaction_mode="create_savepoint"`: los `commit()` del test son
#     SAVEPOINTs; al terminar, el rollback externo deja la DB prístina.
#   - Aislamiento entre tests = rollback; entre workers de xdist = proceso
#     distinto (cada uno tiene su propia DB in-memory).
#
# Si un test necesita crear sus PROPIAS sesiones/factories sobre el engine
# (commits reales fuera de la transacción del test), usá `isolated_db_engine`.
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture(scope="session")
async def db_engine() -> AsyncGenerator[AsyncEngine, None]:
    from sqlalchemy import event  # noqa: PLC0415
    from sqlalchemy.pool import StaticPool  # noqa: PLC0415

    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=StaticPool)

    # Receta oficial de SQLAlchemy para SAVEPOINTs sobre pysqlite/aiosqlite:
    # su emulación de transacciones comitea implícitamente (rompería el patrón
    # rollback-por-test dejando fugas entre tests). Se desactiva y SQLAlchemy
    # emite BEGIN explícito.
    @event.listens_for(engine.sync_engine, "connect")
    def _do_connect(dbapi_connection, connection_record):
        dbapi_connection.isolation_level = None

    @event.listens_for(engine.sync_engine, "begin")
    def _do_begin(conn):
        conn.exec_driver_sql("BEGIN")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    async with db_engine.connect() as conn:
        trans = await conn.begin()
        session = AsyncSession(
            bind=conn,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield session
        finally:
            await session.close()
            if trans.is_active:
                await trans.rollback()


def _f5_unique_index_names() -> tuple[str, ...]:
    """Los uniques que declara F5-B, LEÍDOS del ORM (products ×2 + inventory_balances).

    Derivados y no hardcodeados a propósito: con la lista escrita a mano, renombrar un
    índice en el modelo y en la migración deja este ``DROP INDEX IF EXISTS`` sin
    encontrar nada, la fixture se vuelve un no-op SILENCIOSO y los tests que la piden
    pasan a correr CON los uniques puestos — verdes por la razón equivocada, que es el
    modo de falla que F5-B ya se comió dos veces. Derivándolos, un rename se propaga
    solo; si un día no quedara ninguno, el ``assert`` lo grita.
    """
    from app.persistence.models.inventory import InventoryBalance  # noqa: PLC0415
    from app.persistence.models.product import Product  # noqa: PLC0415

    names = tuple(
        index.name
        for model in (Product, InventoryBalance)
        for index in sorted(model.__table__.indexes, key=lambda i: i.name or "")
        if index.unique and index.name
    )
    assert names, "F5-B declara uniques en el ORM; si esto queda vacío, algo se borró"
    return names


_F5_UNIQUE_INDEXES = _f5_unique_index_names()


@pytest_asyncio.fixture
async def legacy_pre_f5_schema(db_session: AsyncSession) -> AsyncGenerator[None, None]:
    """Dropea los uniques de F5-B: esquema PRE-migración, para escenarios históricos.

    Explícita a propósito (nunca ``autouse``): el default del suite es el esquema
    POST-F5, que es el que corre en producción una vez aplicada ``20260802_0001``.
    Solo la piden los tests cuyo escenario **no puede existir** con los índices
    puestos — dos productos ACTIVOS compartiendo sku/barcode, que es justamente lo
    que el dedup de F3 fusiona y lo que el índice vuelve imposible.

    NO recrea los índices en el teardown, y es deliberado: en SQLite el DDL es
    transaccional y el ``DROP`` corre DENTRO de la transacción externa de
    ``db_session``, así que el rollback de fin de test los restaura solo (probado en
    ``test_legacy_pre_f5_schema_no_contamina``). Recrearlos a mano acá sería peor
    que redundante: el teardown de la fixture corre ANTES de ese rollback, con las
    filas duplicadas del test todavía vivas, así que el ``CREATE UNIQUE INDEX``
    fallaría con IntegrityError exactamente en los tests de dedup — los únicos que
    necesitan esta fixture.
    """
    for name in _F5_UNIQUE_INDEXES:
        await db_session.execute(text(f"DROP INDEX IF EXISTS {name}"))
    await db_session.flush()
    yield


@pytest_asyncio.fixture
async def isolated_db_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Engine efímero con schema propio (el patrón viejo, LENTO — ~2s por test).

    Solo para tests que crean sus propias sessions/factories sobre el engine y
    commitean de verdad: en el engine compartido eso fugaría datos entre tests.
    """
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


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
