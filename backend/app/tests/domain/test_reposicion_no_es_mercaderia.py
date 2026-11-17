"""«Reposición de caja» es plata que pone un socio, no mercadería: el alias suelto
«reposicion» la mandaba a INVENTORY y terminaba como «compra sin artículos»."""

from __future__ import annotations

import pytest

from app.domain.expense_categories import normalize_expense_category


@pytest.mark.parametrize("texto", ["Reposición de caja", "reposicion", "Reposición de fondos"])
def test_reposicion_a_secas_no_es_inventario(texto: str) -> None:
    assert normalize_expense_category(texto)[0] != "INVENTORY"


@pytest.mark.parametrize(
    "texto",
    ["Reposición de mercadería", "reposicion de stock", "Reposición de inventario"],
)
def test_reposicion_de_lo_que_se_vende_sigue_siendo_inventario(texto: str) -> None:
    assert normalize_expense_category(texto)[0] == "INVENTORY"
