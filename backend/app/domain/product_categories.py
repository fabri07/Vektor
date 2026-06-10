"""Catálogos canónicos de categorías de producto por vertical + normalizador.

Mismo mecanismo que ``expense_categories`` (reusa ``CategoryNormalizer``), pero
el catálogo depende del vertical del negocio (``BusinessProfile.business_type``).
Texto sin match → ``OTHER`` preservando el original como label en
``custom_fields["category_label"]``. Vertical desconocido → ``kiosco_almacen``
(mismo fallback que ``HeuristicEngine``).
"""

from __future__ import annotations

from app.domain.expense_categories import CategoryNormalizer

_FALLBACK_VERTICAL = "kiosco_almacen"

# ── Catálogos por vertical ────────────────────────────────────────────────────

PRODUCT_CATEGORY_LABELS: dict[str, dict[str, str]] = {
    "kiosco_almacen": {
        "BEBIDAS": "Bebidas",
        "GOLOSINAS": "Golosinas",
        "CIGARRILLOS": "Cigarrillos",
        "ALMACEN": "Almacén",
        "LACTEOS": "Lácteos",
        "PANIFICADOS": "Panificados",
        "SNACKS": "Snacks",
        "LIMPIEZA": "Limpieza",
        "PERFUMERIA": "Perfumería",
        "OTHER": "Otros",
    },
    "limpieza": {
        "DETERGENTES": "Detergentes y jabones",
        "QUIMICOS": "Químicos y desinfectantes",
        "PAPEL": "Papel y descartables",
        "ACCESORIOS": "Accesorios e implementos",
        "AEROSOLES": "Aerosoles y fragancias",
        "BOLSAS": "Bolsas de residuo",
        "OTHER": "Otros",
    },
    "decoracion_hogar": {
        "TEXTILES": "Textiles",
        "ILUMINACION": "Iluminación",
        "MUEBLES": "Muebles",
        "DECO": "Objetos de decoración",
        "BAZAR": "Bazar y cocina",
        "JARDIN": "Jardín y exterior",
        "AROMAS": "Aromas y velas",
        "OTHER": "Otros",
    },
}

_ALIASES: dict[str, dict[str, str]] = {
    "kiosco_almacen": {
        "bebida": "BEBIDAS",
        "gaseosas": "BEBIDAS",
        "gaseosa": "BEBIDAS",
        "aguas": "BEBIDAS",
        "cervezas": "BEBIDAS",
        "cerveza": "BEBIDAS",
        "vinos": "BEBIDAS",
        "jugos": "BEBIDAS",
        "energizantes": "BEBIDAS",
        "golosina": "GOLOSINAS",
        "caramelos": "GOLOSINAS",
        "chocolates": "GOLOSINAS",
        "chocolate": "GOLOSINAS",
        "alfajores": "GOLOSINAS",
        "alfajor": "GOLOSINAS",
        "chicles": "GOLOSINAS",
        "cigarrillo": "CIGARRILLOS",
        "tabaco": "CIGARRILLOS",
        "puchos": "CIGARRILLOS",
        "almacen": "ALMACEN",
        "comestibles": "ALMACEN",
        "fideos": "ALMACEN",
        "arroz": "ALMACEN",
        "conservas": "ALMACEN",
        "yerba": "ALMACEN",
        "lacteo": "LACTEOS",
        "leche": "LACTEOS",
        "quesos": "LACTEOS",
        "yogur": "LACTEOS",
        "fiambres": "LACTEOS",
        "pan": "PANIFICADOS",
        "facturas": "PANIFICADOS",
        "galletitas": "SNACKS",
        "snack": "SNACKS",
        "papas fritas": "SNACKS",
        "mani": "SNACKS",
        "limpieza hogar": "LIMPIEZA",
        "higiene": "PERFUMERIA",
        "perfumeria": "PERFUMERIA",
        "cosmetica": "PERFUMERIA",
    },
    "limpieza": {
        "detergente": "DETERGENTES",
        "jabon": "DETERGENTES",
        "jabones": "DETERGENTES",
        "suavizantes": "DETERGENTES",
        "lavandina": "QUIMICOS",
        "cloro": "QUIMICOS",
        "desinfectante": "QUIMICOS",
        "desengrasante": "QUIMICOS",
        "quimico": "QUIMICOS",
        "papel higienico": "PAPEL",
        "servilletas": "PAPEL",
        "rollos": "PAPEL",
        "descartables": "PAPEL",
        "trapos": "ACCESORIOS",
        "escobas": "ACCESORIOS",
        "cepillos": "ACCESORIOS",
        "guantes": "ACCESORIOS",
        "esponjas": "ACCESORIOS",
        "accesorio": "ACCESORIOS",
        "aerosol": "AEROSOLES",
        "desodorante de ambiente": "AEROSOLES",
        "fragancias": "AEROSOLES",
        "bolsa": "BOLSAS",
        "residuos": "BOLSAS",
        "consorcio": "BOLSAS",
    },
    "decoracion_hogar": {
        "textil": "TEXTILES",
        "almohadones": "TEXTILES",
        "cortinas": "TEXTILES",
        "mantas": "TEXTILES",
        "alfombras": "TEXTILES",
        "sabanas": "TEXTILES",
        "lampara": "ILUMINACION",
        "lamparas": "ILUMINACION",
        "luces": "ILUMINACION",
        "veladores": "ILUMINACION",
        "mueble": "MUEBLES",
        "sillas": "MUEBLES",
        "mesas": "MUEBLES",
        "estanterias": "MUEBLES",
        "decoracion": "DECO",
        "cuadros": "DECO",
        "espejos": "DECO",
        "floreros": "DECO",
        "adornos": "DECO",
        "bazar": "BAZAR",
        "cocina": "BAZAR",
        "vajilla": "BAZAR",
        "copas": "BAZAR",
        "tazas": "BAZAR",
        "jardin": "JARDIN",
        "macetas": "JARDIN",
        "exterior": "JARDIN",
        "velas": "AROMAS",
        "sahumerios": "AROMAS",
        "difusores": "AROMAS",
        "aromatizantes": "AROMAS",
    },
}

# Alias de verticales (mismo criterio que heuristics/verticals/loader.py).
_VERTICAL_ALIASES = {"kiosco": "kiosco_almacen"}

_normalizers: dict[str, CategoryNormalizer] = {}


def _resolve_vertical(business_type: str | None) -> str:
    if business_type is None:
        return _FALLBACK_VERTICAL
    key = business_type.strip().lower()
    key = _VERTICAL_ALIASES.get(key, key)
    return key if key in PRODUCT_CATEGORY_LABELS else _FALLBACK_VERTICAL


def _get_normalizer(vertical: str) -> CategoryNormalizer:
    if vertical not in _normalizers:
        labels = PRODUCT_CATEGORY_LABELS[vertical]
        # Los labels canónicos también son alias ("Bebidas" → BEBIDAS).
        aliases = {**_ALIASES.get(vertical, {}), **{v: k for k, v in labels.items()}}
        _normalizers[vertical] = CategoryNormalizer(
            codes=tuple(labels.keys()),
            aliases=aliases,
            fallback_code="OTHER",
        )
    return _normalizers[vertical]


def product_category_catalog(business_type: str | None) -> list[dict[str, str]]:
    """Catálogo del vertical como lista [{code, label}] (para la API/frontend)."""
    vertical = _resolve_vertical(business_type)
    return [
        {"code": code, "label": label}
        for code, label in PRODUCT_CATEGORY_LABELS[vertical].items()
    ]


def normalize_product_category(
    raw: str | None, business_type: str | None
) -> tuple[str, str | None]:
    """Texto libre → ``(código canónico del vertical, label)``.

    ``label`` viene poblado solo cuando no hubo match (código OTHER), para
    preservar el texto original.
    """
    return _get_normalizer(_resolve_vertical(business_type)).normalize(raw)
