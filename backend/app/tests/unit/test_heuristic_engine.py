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

_HEURISTICS_DIR = (
    Path(__file__).resolve().parents[2] / "application" / "data" / "heuristics"
)


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


@pytest.mark.parametrize("vertical", list(Vertical))
def test_default_carga_el_json_del_rubro(vertical: Vertical) -> None:
    """El engine de los agentes lee el MISMO archivo que el health engine.

    Se compara contra el JSON, no contra números escritos acá: con valores
    congelados, recalibrar un rubro contra su fuente sectorial rompía este test
    aunque el engine siguiera leyendo perfecto. Lo que importa es que las dos
    lecturas del mismo archivo no diverjan — si divergen, los agentes narran un
    rango y el score usa otro.
    """
    data = json.loads(
        (_HEURISTICS_DIR / f"{vertical.value}.json").read_text(encoding="utf-8")
    )
    config = HeuristicEngine.get(vertical)

    assert config.margin.net_expected_min == data["margin"]["net_expected_min"]
    assert config.margin.net_expected_max == data["margin"]["net_expected_max"]
    assert config.cash_health.critical_days_below == data["cash_health"]["critical_days_below"]
    assert config.cash_health.healthy_days_min == data["cash_health"]["healthy_days_min"]
    assert config.inventory.rotation_days_min == data["inventory"]["rotation_days_min"]
    assert config.inventory.rotation_days_max == data["inventory"]["rotation_days_max"]


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
    """Sobrestock = más del doble del techo de rotación del rubro.

    El umbral se deriva de `rotation_days_max` en vez de fijarse: la regla es
    "el doble del techo", y esa regla no cambia cuando el rubro se recalibra.
    """
    config = HeuristicEngine.get(Vertical.DECORACION_HOGAR)
    umbral = config.inventory.rotation_days_max * 2

    assert config.is_overstock(umbral + 40) is True
    assert config.is_overstock(umbral - 110) is False
    assert config.is_overstock(umbral) is False  # exactamente en el límite, no supera


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

    default = HeuristicEngine.get(Vertical.KIOSCO_ALMACEN)
    assert config.margin.net_expected_min == 0.30
    # El resto de los valores deben seguir siendo los defaults del rubro
    assert config.margin.net_expected_max == default.margin.net_expected_max
    assert config.cash_health.critical_days_below == default.cash_health.critical_days_below
