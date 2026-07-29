"""Catálogos canónicos de categorías de producto por vertical + normalizador.

Mismo mecanismo que ``expense_categories`` (reusa ``CategoryNormalizer``), pero
el catálogo depende del vertical del negocio (``BusinessProfile.business_type``).
Texto sin match → ``OTHER`` preservando el original como label en
``custom_fields["category_label"]``. El vertical llega tipado (``Vertical``): un
código desconocido lo rechaza ``parse_vertical`` en el borde, nunca se traduce a
otro rubro.

Una categoría ≠ ``OTHER`` para el vertical es la señal que ``expense_categories.
classify_expense_with_vertical`` usa para marcar un gasto como mercadería de
reventa (INVENTORY/COGS). Por eso el catálogo cubre la mercadería realmente
vendida del vertical (en kiosco: diarios/revistas y regalería/accesorios además
de bebidas, golosinas, etc.). Los casos genuinamente ambiguos reventa-vs-insumo
—p. ej. accesorios de limpieza (trapos, guantes) que un kiosco puede consumir
internamente o revender— se dejan en su default y se reclasifican vía chat; no se
fuerzan acá para no convertir un insumo operativo en COGS por error.
"""

from __future__ import annotations

from app.domain.expense_categories import CategoryNormalizer
from app.domain.verticals import Vertical

# ── Catálogos por vertical ────────────────────────────────────────────────────

PRODUCT_CATEGORY_LABELS: dict[Vertical, dict[str, str]] = {
    Vertical.KIOSCO_ALMACEN: {
        "BEBIDAS": "Bebidas",
        "GOLOSINAS": "Golosinas",
        "CIGARRILLOS": "Cigarrillos",
        "ALMACEN": "Almacén",
        "LACTEOS": "Lácteos",
        "PANIFICADOS": "Panificados",
        "SNACKS": "Snacks",
        "LIMPIEZA": "Limpieza",
        "PERFUMERIA": "Perfumería",
        "DIARIOS_REVISTAS": "Diarios y revistas",
        "REGALERIA": "Regalería y varios",
        "OTHER": "Otros",
    },
    Vertical.LIMPIEZA: {
        "DETERGENTES": "Detergentes y jabones",
        "QUIMICOS": "Químicos y desinfectantes",
        "PAPEL": "Papel y descartables",
        "ACCESORIOS": "Accesorios e implementos",
        "AEROSOLES": "Aerosoles y fragancias",
        "BOLSAS": "Bolsas de residuo",
        "OTHER": "Otros",
    },
    Vertical.DECORACION_HOGAR: {
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

_ALIASES: dict[Vertical, dict[str, str]] = {
    Vertical.KIOSCO_ALMACEN: {
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
        "panaderia": "PANIFICADOS",
        "infusiones": "ALMACEN",
        "infusion": "ALMACEN",
        # Farmacia básica de kiosco (curitas, OTC, preservativos) → perfumería/higiene.
        "medicamentos": "PERFUMERIA",
        "medicamentos otc": "PERFUMERIA",
        "salud": "PERFUMERIA",
        "farmacia": "PERFUMERIA",
        "galletitas": "SNACKS",
        "snack": "SNACKS",
        "papas fritas": "SNACKS",
        "mani": "SNACKS",
        "limpieza hogar": "LIMPIEZA",
        "higiene": "PERFUMERIA",
        "perfumeria": "PERFUMERIA",
        "cosmetica": "PERFUMERIA",
        # Diarios y revistas (mercadería de reventa de kiosco). Cabeceras locales
        # frecuentes se listan como alias para que el import las marque COGS.
        "diario": "DIARIOS_REVISTAS",
        "diarios": "DIARIOS_REVISTAS",
        "revista": "DIARIOS_REVISTAS",
        "revistas": "DIARIOS_REVISTAS",
        "periodico": "DIARIOS_REVISTAS",
        "suplemento": "DIARIOS_REVISTAS",
        "la nacion": "DIARIOS_REVISTAS",
        "clarin": "DIARIOS_REVISTAS",
        "ole": "DIARIOS_REVISTAS",
        "pagina 12": "DIARIOS_REVISTAS",
        # Regalería y accesorios vendibles que hoy caían a OTHER/OPEX (de capturas
        # reales). Librería vendible (cuaderno, lapicera) entra acá como reventa.
        "regaleria": "REGALERIA",
        "auricular": "REGALERIA",
        "auriculares": "REGALERIA",
        "pila": "REGALERIA",
        "pilas": "REGALERIA",
        "encendedor": "REGALERIA",
        "encendedores": "REGALERIA",
        "fosforo": "REGALERIA",
        "fosforos": "REGALERIA",
        "cuaderno": "REGALERIA",
        "lapicera": "REGALERIA",
        "cargador": "REGALERIA",
    },
    Vertical.LIMPIEZA: {
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
    Vertical.DECORACION_HOGAR: {
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

_normalizers: dict[Vertical, CategoryNormalizer] = {}


def _get_normalizer(vertical: Vertical) -> CategoryNormalizer:
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


def product_category_catalog(vertical: Vertical) -> list[dict[str, str]]:
    """Catálogo del vertical como lista [{code, label}] (para la API/frontend)."""
    return [
        {"code": code, "label": label}
        for code, label in PRODUCT_CATEGORY_LABELS[vertical].items()
    ]


def normalize_product_category(raw: str | None, vertical: Vertical) -> tuple[str, str | None]:
    """Texto libre → ``(código canónico del vertical, label)``.

    ``label`` viene poblado solo cuando no hubo match (código OTHER), para
    preservar el texto original.
    """
    return _get_normalizer(vertical).normalize(raw)
