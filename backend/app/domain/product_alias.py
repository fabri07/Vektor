"""Alias de nombre persistido para un producto (F-S.0, mecanismo 3).

Un alias es un nombre con el que un ARCHIVO llamó al producto y que el
usuario vinculó a mano a uno ya existente — nunca se infiere solo, es
exactamente lo opuesto a inventar: es la decisión humana que ya se tomó una
vez, guardada para no repetirla. Vive en ``custom_fields["_aliases"]`` porque
no es un dato de negocio del producto (no aparece en la ficha), es una pista
de matching para la PRÓXIMA importación. Mismo patrón de flag en
``custom_fields`` que ``_sentinel``/``_brand_collapsed``/``_vektor_costo_base``.
"""

from __future__ import annotations

from typing import Any

from app.domain.text_norm import normalize_product_name

ALIASES_FIELD = "_aliases"

#: Mismo límite que el resto de campos de texto de producto (`_clean_str(name, 299)`
#: en `ingestion_import_service.py`).
MAX_ALIAS_LENGTH = 299

#: Tope defensivo, no una regla de negocio. Pasado esto, `add_alias` no agrega
#: más en silencio a propósito: el dato real (la venta que pidió el alias) no
#: se pierde por no sumar un alias #21 — sólo significa que esa venta puntual
#: sigue resolviendo por vinculación manual una vez más.
MAX_ALIASES_PER_PRODUCT = 20


def product_aliases(custom_fields: dict[str, Any] | None) -> list[str]:
    """Alias guardados de un producto, tolerante a datos legacy o corruptos.

    ``custom_fields["_aliases"]`` sólo es válido como ``list[str]``. Cualquier
    otra forma — un string suelto, un número, un dict, una lista con
    elementos que no son string — se trata como "no hay alias" en vez de
    propagar el dato roto: un ``list("abc")`` sobre un string legacy daría
    tres alias de una letra cada uno.
    """
    raw = (custom_fields or {}).get(ALIASES_FIELD)
    if not isinstance(raw, list):
        return []
    return [item.strip() for item in raw if isinstance(item, str) and item.strip()]


def add_alias(custom_fields: dict[str, Any] | None, raw_name: str) -> dict[str, Any]:
    """``custom_fields`` NUEVO con ``raw_name`` agregado a los alias.

    No muta el dict de entrada (reasignación completa — no hay ``MutableDict``
    en el modelo `Product`, así que una mutación in-place no se detectaría
    como cambio). Idempotente por forma NORMALIZADA: "Coca" y "COCA" son el
    mismo alias, y se conserva la primera forma cruda que se guardó (para no
    reemplazarla por otra variante de mayúsculas en pantalla). Si el valor
    almacenado estaba corrupto (ver `product_aliases`), esta llamada lo repara:
    escribe una lista limpia en vez de propagar la forma inválida.
    """
    cleaned = raw_name.strip()[:MAX_ALIAS_LENGTH]
    base = dict(custom_fields or {})
    if not cleaned:
        return base
    existing = product_aliases(custom_fields)
    norm_nuevo = normalize_product_name(cleaned)
    if any(normalize_product_name(a) == norm_nuevo for a in existing):
        return base
    if len(existing) >= MAX_ALIASES_PER_PRODUCT:
        return base
    return {**base, ALIASES_FIELD: [*existing, cleaned]}
