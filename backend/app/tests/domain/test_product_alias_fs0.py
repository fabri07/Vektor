"""F-S.0 mecanismo 3: el alias es una decisión humana persistida, nunca
inferida. `add_alias` es puro (no muta el dict de entrada), idempotente por
forma normalizada, y `product_aliases` tolera datos legacy/corruptos sin
propagarlos.
"""

from __future__ import annotations

from app.domain.product_alias import (
    ALIASES_FIELD,
    MAX_ALIASES_PER_PRODUCT,
    add_alias,
    product_aliases,
)


def test_add_alias_agrega_a_una_lista_nueva() -> None:
    result = add_alias(None, "Gaseosa cola cualquiera")
    assert result == {ALIASES_FIELD: ["Gaseosa cola cualquiera"]}


def test_add_alias_no_muta_el_dict_de_entrada() -> None:
    original = {ALIASES_FIELD: ["ya existía"]}
    result = add_alias(original, "nuevo alias")
    assert original == {ALIASES_FIELD: ["ya existía"]}, "no se puede mutar in-place"
    assert result == {ALIASES_FIELD: ["ya existía", "nuevo alias"]}


def test_add_alias_es_idempotente_por_forma_exacta() -> None:
    once = add_alias(None, "Coca")
    twice = add_alias(once, "Coca")
    assert twice == once


def test_add_alias_es_idempotente_por_forma_normalizada() -> None:
    """Mayúsculas/acentos distintos del MISMO alias no duplican — se
    conserva la primera forma cruda guardada."""
    once = add_alias(None, "Gaseosa Cólá")
    twice = add_alias(once, "gaseosa cola")
    assert twice == once
    assert product_aliases(twice) == ["Gaseosa Cólá"]


def test_add_alias_ignora_nombre_vacio() -> None:
    result = add_alias({"marca": "Coca-Cola"}, "   ")
    assert result == {"marca": "Coca-Cola"}


def test_add_alias_preserva_otras_claves() -> None:
    result = add_alias({"marca": "Coca-Cola"}, "Gaseosa")
    assert result == {"marca": "Coca-Cola", ALIASES_FIELD: ["Gaseosa"]}


def test_add_alias_cap_de_longitud() -> None:
    largo = "x" * 500
    result = add_alias(None, largo)
    assert len(product_aliases(result)[0]) == 299


def test_add_alias_cap_de_cantidad_no_agrega_mas_alla_del_tope() -> None:
    cf: dict[str, object] = None  # type: ignore[assignment]
    for i in range(MAX_ALIASES_PER_PRODUCT):
        cf = add_alias(cf, f"alias {i}")
    assert len(product_aliases(cf)) == MAX_ALIASES_PER_PRODUCT
    cf = add_alias(cf, "alias de mas, no entra")
    assert len(product_aliases(cf)) == MAX_ALIASES_PER_PRODUCT
    assert "alias de mas, no entra" not in product_aliases(cf)


def test_product_aliases_vacio_sin_datos() -> None:
    assert product_aliases(None) == []
    assert product_aliases({}) == []


def test_product_aliases_ignora_dato_legacy_string() -> None:
    """`custom_fields["_aliases"]` guardado como string suelto (dato legacy o
    corrupto) no puede convertirse en una lista de caracteres."""
    assert product_aliases({ALIASES_FIELD: "Coca"}) == []


def test_product_aliases_ignora_elementos_que_no_son_string() -> None:
    assert product_aliases({ALIASES_FIELD: ["Coca", 123, None, {"x": 1}]}) == ["Coca"]


def test_add_alias_repara_un_valor_corrupto() -> None:
    """Si lo guardado no era una lista válida, la próxima escritura lo
    reemplaza por una lista limpia en vez de propagar la forma inválida."""
    result = add_alias({ALIASES_FIELD: "dato_corrupto_legacy"}, "Gaseosa")
    assert product_aliases(result) == ["Gaseosa"]
