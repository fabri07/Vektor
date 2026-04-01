"""
Tests — HeuristicEngine completo (FASE 2B).

Los primeros 8 tests son puramente síncronos (sin BD).
test_async_override_applies usa SQLite in-memory para verificar que
los overrides almacenados en business_heuristic_overrides se aplican correctamente.
"""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.persistence.models  # noqa: F401 — registra todos los modelos en Base
from app.application.agents.shared.heuristic_engine import HeuristicEngine
from app.persistence.db.base import Base
from app.persistence.models.heuristic_override import BusinessHeuristicOverride
from app.persistence.models.tenant import Tenant

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


# ── Fixtures SQLite in-memory ─────────────────────────────────────────────────

@pytest_asyncio.fixture
async def sqlite_engine():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def sqlite_session(sqlite_engine):
    factory = async_sessionmaker(sqlite_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session


# ── Tests síncronos (sin BD) ──────────────────────────────────────────────────

def test_default_kiosco_loads():
    config = HeuristicEngine.get("kiosco_almacen")
    assert config.margin.net_expected_min == 0.12
    assert config.margin.net_expected_max == 0.18
    assert config.cash_health.critical_days_below == 5
    assert config.inventory.rotation_days_min == 7


def test_default_limpieza_loads():
    config = HeuristicEngine.get("limpieza")
    assert config.margin.net_expected_min == 0.18
    assert config.margin.net_expected_max == 0.28
    assert config.cash_health.healthy_days_min == 20
    assert config.inventory.rotation_days_max == 45


def test_default_decoracion_loads():
    config = HeuristicEngine.get("decoracion_hogar")
    assert config.margin.net_expected_min == 0.25
    assert config.margin.net_expected_max == 0.45
    assert config.cash_health.healthy_days_min == 30
    assert config.inventory.rotation_days_max == 180


def test_prompt_fragment_contains_numbers():
    config = HeuristicEngine.get("kiosco_almacen")
    fragment = config.to_prompt_fragment()
    assert "%" in fragment
    assert any(ch.isdigit() for ch in fragment)
    # REGLA CRÍTICA: nunca texto narrativo
    assert "saludable" not in fragment.lower()
    assert "bueno" not in fragment.lower()


def test_prompt_fragment_has_all_params():
    config = HeuristicEngine.get("kiosco_almacen")
    fragment = config.to_prompt_fragment()
    # Debe incluir días de cobertura de caja
    assert "días de cobertura" in fragment
    # Debe incluir rotación de inventario
    assert "días" in fragment
    assert "Rotación" in fragment
    # Debe incluir porcentaje de margen
    assert "Margen" in fragment
    assert "%" in fragment
    # Debe incluir sección crítica
    assert "Crítico" in fragment


def test_overstock_detection():
    """
    decoracion_hogar: rotation_days_max=180, threshold=180*2=360.
    400 > 360 → overstock. 250 < 360 → no overstock.
    """
    config = HeuristicEngine.get("decoracion_hogar")
    assert config.is_overstock(400) is True
    assert config.is_overstock(250) is False
    assert config.is_overstock(360) is False  # exactamente en el límite, no supera


def test_cash_critical():
    """
    kiosco: critical_days_below=5.
    3 < 5 → crítico. 6 >= 5 → no crítico.
    """
    config = HeuristicEngine.get("kiosco_almacen")
    assert config.is_cash_critical(3) is True
    assert config.is_cash_critical(6) is False
    assert config.is_cash_critical(5) is False  # exactamente en el límite, no es crítico


def test_unknown_business_type_uses_fallback():
    """Un rubro desconocido ("ferreteria") carga sin error usando el fallback de kiosco."""
    config = HeuristicEngine.get("ferreteria")
    assert config is not None
    # Fallback usa kiosco_almacen defaults
    assert config.margin.net_expected_min == 0.12
    assert config.inventory.rotation_days_min == 7


# ── Test asíncrono con override en BD ────────────────────────────────────────

@pytest.mark.asyncio
async def test_async_override_applies(sqlite_session: AsyncSession):
    """
    Insertar override margin.net_expected_min=0.30 para un tenant,
    verificar que get_async() lo aplica sobre el default (0.12).
    """
    tenant_id = uuid.uuid4()

    # Insertar tenant requerido por la FK
    tenant = Tenant(
        tenant_id=tenant_id,
        legal_name="Test Override Kiosco",
        display_name="Test Override",
        currency="ARS",
        pricing_reference_mode="MEP",
        status="ACTIVE",
    )
    sqlite_session.add(tenant)

    # Insertar override
    override = BusinessHeuristicOverride(
        tenant_id=tenant_id,
        param_key="margin.net_expected_min",
        param_value=0.30,
    )
    sqlite_session.add(override)
    await sqlite_session.commit()

    config = await HeuristicEngine.get_async(
        "kiosco_almacen", str(tenant_id), sqlite_session
    )

    assert config.margin.net_expected_min == 0.30
    # El resto de los valores deben seguir siendo los defaults
    assert config.margin.net_expected_max == 0.18
    assert config.cash_health.critical_days_below == 5
