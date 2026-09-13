"""Invariantes estructurales del catálogo estático de Cambio 1
(docs/plans/conservacion-y-acceso-datos-negocio.md).

Puramente de dominio (sin DB): valida que el catálogo hardcodeado no se haya
tipeado mal — cada `field_id` tiene que ser exactamente lo que produce
`field_id_for(entity_type, value_path)`, porque eso es lo que la identidad
promete (ver el docstring de esa función). Un typo acá rompería la
deduplicación en silencio, exactamente el bug que motivó la revisión.
"""

from __future__ import annotations

import pytest

from app.domain.business_field_catalog import (
    AVAILABLE_ENTITY_TYPES,
    BUSINESS_FIELD_CATALOG,
    descriptors_for,
    field_id_for,
)


@pytest.mark.parametrize("entity_type", sorted(BUSINESS_FIELD_CATALOG))
def test_field_id_coincide_con_field_id_for(entity_type: str) -> None:
    for d in descriptors_for(entity_type):
        assert d.field_id == field_id_for(d.entity_type, d.value_path), (
            f"{entity_type}:{d.field_key} tiene field_id={d.field_id!r} pero "
            f"debería ser {field_id_for(d.entity_type, d.value_path)!r}"
        )


@pytest.mark.parametrize("entity_type", sorted(BUSINESS_FIELD_CATALOG))
def test_sin_field_key_duplicada_dentro_de_la_misma_entidad(entity_type: str) -> None:
    keys = [d.field_key for d in descriptors_for(entity_type)]
    assert len(keys) == len(set(keys)), f"{entity_type} tiene field_key repetida: {keys}"


@pytest.mark.parametrize("entity_type", sorted(BUSINESS_FIELD_CATALOG))
def test_sin_value_path_duplicado_dentro_de_la_misma_entidad(entity_type: str) -> None:
    """Dos descriptores ESTÁTICOS apuntando al mismo lugar colapsarían su
    field_id entre sí — eso solo puede pasar por error de tipeo acá, nunca
    por diseño (cada campo del catálogo describe UN lugar)."""
    paths = [d.value_path for d in descriptors_for(entity_type)]
    assert len(paths) == len(set(paths)), f"{entity_type} tiene value_path repetido: {paths}"


def test_todas_las_entidades_disponibles_tienen_catalogo() -> None:
    """Cambio 1 completo para las 5 secciones del plan: ninguna entidad de
    `AVAILABLE_ENTITY_TYPES` debería quedar con el catálogo estático vacío."""
    for entity_type in AVAILABLE_ENTITY_TYPES:
        assert descriptors_for(entity_type), f"{entity_type} no tiene campos estáticos"


@pytest.mark.parametrize(
    ("entity_type", "field_key"),
    [
        ("sale", "product_name"),
        ("sale", "sku"),
        ("sale", "customer_name"),
        ("sale", "invoice_number"),
        ("sale", "document_type"),
        ("expense", "product_name"),
        ("expense", "quantity"),
        ("expense", "unit_price"),
        ("expense", "shipping_cost"),
        ("expense", "supplier_cuit"),
    ],
)
def test_targets_de_mapeo_que_se_consumen_no_estan_en_el_catalogo(
    entity_type: str, field_key: str
) -> None:
    """Estos SÍ son targets válidos del mapeo de columnas (column_mapping_
    service.CANONICAL_FIELDS) pero NO quedan como valor propio de la fila —
    resuelven un vínculo (product_id/customer_id/supplier_id) o alimentan la
    huella de identidad de operación / otro efecto (F-H6.a). Publicarlos acá
    prometería una lectura que no existe."""
    keys = {d.field_key for d in descriptors_for(entity_type)}
    assert field_key not in keys
