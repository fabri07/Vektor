"""
Tests — HeuristicEngine completo (FASE 2B).

Los primeros 8 tests son puramente síncronos (sin BD).
test_async_override_applies usa SQLite in-memory para verificar que
los overrides almacenados en business_heuristic_overrides se aplican correctamente.
"""

import json
import shutil
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.persistence.models  # noqa: F401 — registra todos los modelos en Base
from app.application.agents.shared import heuristic_engine
from app.application.agents.shared.heuristic_engine import HeuristicEngine
from app.domain.verticals import Vertical
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
    config = HeuristicEngine.get(Vertical.KIOSCO_ALMACEN)
    assert config.margin.net_expected_min == 0.12
    assert config.margin.net_expected_max == 0.18
    assert config.cash_health.critical_days_below == 5
    assert config.inventory.rotation_days_min == 7


def test_default_limpieza_loads():
    config = HeuristicEngine.get(Vertical.LIMPIEZA)
    assert config.margin.net_expected_min == 0.18
    assert config.margin.net_expected_max == 0.28
    assert config.cash_health.healthy_days_min == 20
    assert config.inventory.rotation_days_max == 45


def test_default_decoracion_loads():
    config = HeuristicEngine.get(Vertical.DECORACION_HOGAR)
    assert config.margin.net_expected_min == 0.25
    assert config.margin.net_expected_max == 0.45
    assert config.cash_health.healthy_days_min == 30
    assert config.inventory.rotation_days_max == 180


def test_prompt_fragment_contains_numbers():
    config = HeuristicEngine.get(Vertical.KIOSCO_ALMACEN)
    fragment = config.to_prompt_fragment()
    assert "%" in fragment
    assert any(ch.isdigit() for ch in fragment)
    # REGLA CRÍTICA: nunca texto narrativo
    assert "saludable" not in fragment.lower()
    assert "bueno" not in fragment.lower()


def test_prompt_fragment_has_all_params():
    config = HeuristicEngine.get(Vertical.KIOSCO_ALMACEN)
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
    config = HeuristicEngine.get(Vertical.DECORACION_HOGAR)
    assert config.is_overstock(400) is True
    assert config.is_overstock(250) is False
    assert config.is_overstock(360) is False  # exactamente en el límite, no supera


def test_cash_critical():
    """
    kiosco: critical_days_below=5.
    3 < 5 → crítico. 6 >= 5 → no crítico.
    """
    config = HeuristicEngine.get(Vertical.KIOSCO_ALMACEN)
    assert config.is_cash_critical(3) is True
    assert config.is_cash_critical(6) is False
    assert config.is_cash_critical(5) is False  # exactamente en el límite, no es crítico


def test_json_faltante_levanta_en_vez_de_servir_el_de_otro_rubro(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """El JSON del rubro es un contrato de deploy: si falta, `get` levanta.

    Esto es lo que reemplazó al viejo `_load_default` con fallback a kiosco. NO
    se prueba con `HeuristicEngine.get(parse_vertical("kiosco"))`: ahí la
    excepción la tira `parse_vertical` al evaluar el argumento y el SUT ni
    siquiera se ejecuta — el test quedaría verde aunque se borrara el cuerpo
    entero de `get`. El rechazo del alias legado vive en `test_verticals.py`,
    donde sí mide a `parse_vertical`.

    El directorio temporal tiene el JSON de kiosco y NO el de limpieza a
    propósito: con un directorio vacío, un `get` que volviera a caer al JSON de
    kiosco levantaría igual (por el archivo de destino, que tampoco estaría) y
    el test pasaría por la razón equivocada. Así, un fallback silencioso
    devuelve el config de kiosco y el test lo caza.
    """
    shutil.copy(
        heuristic_engine.DATA_DIR / f"{Vertical.KIOSCO_ALMACEN.value}.json",
        tmp_path / f"{Vertical.KIOSCO_ALMACEN.value}.json",
    )
    monkeypatch.setattr(heuristic_engine, "DATA_DIR", tmp_path)

    with pytest.raises(FileNotFoundError):
        HeuristicEngine.get(Vertical.LIMPIEZA)


def test_json_incompleto_levanta(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Los sub-configs no tienen defaults a propósito (los que había eran los de
    kiosco): a un JSON con una clave-hoja faltante ya no se le inyectan los
    umbrales de otro rubro en silencio, pydantic lo grita."""
    completo = json.loads(
        (heuristic_engine.DATA_DIR / f"{Vertical.KIOSCO_ALMACEN.value}.json").read_text()
    )
    del completo["margin"]["net_expected_min"]
    (tmp_path / f"{Vertical.KIOSCO_ALMACEN.value}.json").write_text(json.dumps(completo))
    monkeypatch.setattr(heuristic_engine, "DATA_DIR", tmp_path)

    with pytest.raises(ValidationError):
        HeuristicEngine.get(Vertical.KIOSCO_ALMACEN)


# ── Test asíncrono con override en BD ────────────────────────────────────────


@pytest.mark.asyncio
async def test_async_override_applies(sqlite_session: AsyncSession) -> None:
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
        Vertical.KIOSCO_ALMACEN, str(tenant_id), sqlite_session
    )

    assert config.margin.net_expected_min == 0.30
    # El resto de los valores deben seguir siendo los defaults
    assert config.margin.net_expected_max == 0.18
    assert config.cash_health.critical_days_below == 5
