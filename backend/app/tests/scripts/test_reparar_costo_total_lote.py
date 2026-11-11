"""`scripts/reparar_costo_total_lote.py`: la parte que decide QUÉ se corrige.

El SQL se probó contra un Postgres descartable (dry-run, apply, segunda corrida
idempotente). Acá se fija lo que no puede salir mal en silencio: la división exacta,
que un costo editado después no se pise, y el formato del costo del catálogo.
"""

from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"


@pytest.fixture(scope="module")
def mod() -> Any:
    if str(_SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "reparar_costo_total_lote", _SCRIPTS_DIR / "reparar_costo_total_lote.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _c(qty: int, mov: str, vigente: str | None) -> dict[str, Any]:
    return {
        "name": "P",
        "qty": qty,
        "mov_cost": Decimal(mov),
        "unit_cost_ars": None if vigente is None else Decimal(vigente),
    }


def test_division_exacta_se_repara(mod: Any) -> None:
    reparar, a_mano = mod.planificar([_c(40, "480000.00", "480000.00")])
    assert a_mano == []
    assert reparar[0]["unitario"] == Decimal("12000.00")


def test_division_inexacta_queda_a_mano_y_no_se_adivina(mod: Any) -> None:
    reparar, a_mano = mod.planificar([_c(7, "100000.00", "100000.00")])
    assert reparar == []
    assert "exacto" in a_mano[0]["motivo"]


def test_un_costo_editado_despues_no_se_pisa(mod: Any) -> None:
    reparar, a_mano = mod.planificar([_c(10, "50000.00", "9999.00")])
    assert reparar == []
    assert "ya no coincide" in a_mano[0]["motivo"]


def test_sin_costo_vigente_queda_a_mano(mod: Any) -> None:
    reparar, a_mano = mod.planificar([_c(10, "50000.00", None)])
    assert reparar == [] and len(a_mano) == 1


def test_una_unidad_o_un_total_cero_no_tienen_unitario(mod: Any) -> None:
    assert mod.proponer_unitario(Decimal("5000"), 1) is None
    assert mod.proponer_unitario(Decimal("0"), 10) is None


def test_el_costo_del_catalogo_se_guarda_sin_notacion_cientifica(mod: Any) -> None:
    assert mod._como_texto(Decimal("12000.00")) == "12000"
    assert mod._como_texto(Decimal("4600.00")) == "4600"
    assert mod._como_texto(Decimal("4500.50")) == "4500.50"
