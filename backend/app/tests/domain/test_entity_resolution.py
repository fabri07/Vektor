"""F-ID.3: `resolve_entity_reference`, puro sobre índices armados a mano.

La regla central: dos identificadores FUERTES de la misma fila que apuntan a
entidades distintas dan `conflict`, nunca gana el primero. "name" es siempre
débil — nunca contradice a un tier fuerte, y si no hay ningún tier fuerte con
datos, un nombre ambiguo es `ambiguous`, uno único es `resolved` igual (es la
mejor evidencia disponible), y nada es `not_found`.
"""

from __future__ import annotations

import uuid

from app.domain.entity_resolution import (
    EntityReferenceIndex,
    resolve_entity_reference,
)

P1 = uuid.UUID("11111111-1111-1111-1111-111111111111")
P2 = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _index(**tiers: dict[str, frozenset[uuid.UUID]]) -> EntityReferenceIndex:
    return EntityReferenceIndex(by_tier=tiers)


def test_resuelve_por_un_solo_tier_fuerte() -> None:
    index = _index(vektor_code={"cli-0001": frozenset({P1})})
    result = resolve_entity_reference("customer", index, {"vektor_code": "cli-0001"})
    assert result.status == "resolved"
    assert result.entity_id == P1
    assert result.matched_by == ("vektor_code",)


def test_tiers_fuertes_que_coinciden_resuelven_juntos() -> None:
    index = _index(
        vektor_code={"cli-0001": frozenset({P1})},
        cuit={"20-11111111-1": frozenset({P1})},
    )
    result = resolve_entity_reference(
        "customer", index, {"vektor_code": "cli-0001", "cuit": "20-11111111-1"}
    )
    assert result.status == "resolved"
    assert result.entity_id == P1
    assert set(result.matched_by) == {"vektor_code", "cuit"}


def test_tiers_fuertes_que_se_contradicen_dan_conflict() -> None:
    index = _index(
        vektor_code={"cli-0001": frozenset({P1})},
        cuit={"20-22222222-2": frozenset({P2})},
    )
    result = resolve_entity_reference(
        "customer", index, {"vektor_code": "cli-0001", "cuit": "20-22222222-2"}
    )
    assert result.status == "conflict"
    assert set(result.candidates) == {P1, P2}
    assert len(result.conflicts) == 1


def test_un_tier_ambiguo_en_si_mismo_da_ambiguous() -> None:
    index = _index(cuit={"20-11111111-1": frozenset({P1, P2})})
    result = resolve_entity_reference("customer", index, {"cuit": "20-11111111-1"})
    assert result.status == "ambiguous"
    assert set(result.candidates) == {P1, P2}


def test_nombre_solo_unico_resuelve() -> None:
    index = _index(name={"juan perez": frozenset({P1})})
    result = resolve_entity_reference("customer", index, {"name": "juan perez"})
    assert result.status == "resolved"
    assert result.entity_id == P1
    assert result.matched_by == ("name",)


def test_nombre_solo_ambiguo_da_ambiguous() -> None:
    index = _index(name={"juan perez": frozenset({P1, P2})})
    result = resolve_entity_reference("customer", index, {"name": "juan perez"})
    assert result.status == "ambiguous"


def test_nombre_nunca_contradice_un_tier_fuerte_resuelto() -> None:
    """El nombre de la fila coincide con OTRO cliente (variante/homónimo) — no
    importa: el código ya resolvió sin ambigüedad, el nombre ni se consulta."""
    index = _index(
        vektor_code={"cli-0001": frozenset({P1})},
        name={"juan perez": frozenset({P2})},
    )
    result = resolve_entity_reference(
        "customer", index, {"vektor_code": "cli-0001", "name": "juan perez"}
    )
    assert result.status == "resolved"
    assert result.entity_id == P1
    assert result.matched_by == ("vektor_code",)


def test_sin_ningun_dato_es_not_found() -> None:
    index = _index(vektor_code={"cli-0001": frozenset({P1})})
    result = resolve_entity_reference("customer", index, {})
    assert result.status == "not_found"


def test_datos_presentes_pero_sin_match_es_not_found() -> None:
    index = _index(vektor_code={"cli-0001": frozenset({P1})})
    result = resolve_entity_reference("customer", index, {"vektor_code": "cli-9999"})
    assert result.status == "not_found"


def test_tiers_vacios_se_ignoran() -> None:
    index = _index(vektor_code={"cli-0001": frozenset({P1})})
    result = resolve_entity_reference(
        "customer", index, {"vektor_code": "cli-0001", "cuit": "", "email": None}  # type: ignore[dict-item]
    )
    assert result.status == "resolved"
    assert result.entity_id == P1


def test_orden_de_precedencia_producto_prioriza_vektor_code_sobre_nombre() -> None:
    index = _index(
        vektor_code={"tex-0001": frozenset({P1})},
        name={"sabana king": frozenset({P2})},
    )
    result = resolve_entity_reference(
        "product", index, {"vektor_code": "tex-0001", "name": "sabana king"}
    )
    assert result.entity_id == P1


def test_orden_de_precedencia_proveedor_usa_cuit_no_dni() -> None:
    index = _index(cuit={"30-11111111-1": frozenset({P1})})
    result = resolve_entity_reference("supplier", index, {"cuit": "30-11111111-1"})
    assert result.status == "resolved"
    assert result.entity_id == P1
