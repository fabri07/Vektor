"""Piezas puras del backfill de recuperación histórica de definiciones
(Cambio 1, docs/plans/conservacion-y-acceso-datos-negocio.md).

El resto del script (`_plan_tenant`/`_apply_tenant`) usa SQL específico de
Postgres (`jsonb_object_keys`) y se verificó a mano en dry-run contra datos
reales de producción (ver el mensaje de la sesión que lo entregó) — mismo
criterio que el resto de `scripts/backfill_*` del repo, ninguno tiene suite
automatizada propia.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

from backfill_field_definitions_from_history import (  # noqa: E402
    RESERVED_KEYS,
    _slug_a_label,
)

from app.domain.business_field_catalog import descriptors_for  # noqa: E402


def test_slug_a_label_es_legible() -> None:
    assert _slug_a_label("purchase_base_cost") == "Purchase base cost"
    assert _slug_a_label("col_8") == "Col 8"
    assert _slug_a_label("marca") == "Marca"


def test_reservadas_no_colisionan_con_el_catalogo_estatico() -> None:
    """Una clave no puede ser a la vez "nunca se publica" y "campo canónico
    con identidad propia" — si colisionaran, `_plan_tenant` la clasificaría
    según el orden de sus `if`, ocultando la ambigüedad en vez de fallarla."""
    estaticas = {d.field_key for entity in RESERVED_KEYS for d in descriptors_for(entity)}
    for entity, reservadas in RESERVED_KEYS.items():
        propias = {d.field_key for d in descriptors_for(entity)}
        assert not (reservadas & propias), f"{entity}: reservada y estática a la vez"
    assert estaticas  # sanity: el catálogo de producto no está vacío


def test_marca_no_esta_reservada() -> None:
    """`marca` es dato de negocio real (Reforma de Proveedores) — el caso que
    motivó este backfill. Si algún día alguien la reserva por error, este test
    lo agarra antes que un dry-run en producción."""
    assert "marca" not in RESERVED_KEYS.get("product", frozenset())
