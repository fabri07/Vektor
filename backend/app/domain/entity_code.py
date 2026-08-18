"""Código Véktor: identificador externo permanente (F-ID, capa 2).

Puro — sin sesión, sin async, mismo criterio que ``product_alias.py``. Resuelve
DOS cosas: qué prefijo le corresponde a una entidad (curado a mano, nunca
inventado) y cómo se formatea el código final (``PREFIJO-NNNN``). NO asigna
el correlativo — eso necesita la base (``entity_code_sequences``, capa async
en ``application/services/entity_code_service.py``) porque tiene que ser
atómico entre tenants concurrentes.

Producto usa ``products.sku`` como su código Véktor (decisión ya cerrada,
2026-08-14: sin migración nueva). Cliente/proveedor usan la columna
denormalizada ``vektor_code``. Los tres comparten este módulo para el
formato/prefijo — no hay tres implementaciones casi-iguales.
"""

from __future__ import annotations

from typing import Literal

from app.domain.verticals import Vertical

EntityKind = Literal["product", "customer", "supplier"]

#: Prefijos planos — no hay eje de categoría real para cliente/proveedor
#: (``customer_type`` persona/empresa es ortogonal, no un catálogo cerrado
#: como las categorías de producto; forzarlo sería complejidad sin necesidad).
CUSTOMER_PREFIX = "CLI"
SUPPLIER_PREFIX = "PRV"

#: Fallback cuando el producto no tiene categoría resuelta (F-CAT sin
#: evidencia) o su categoría es custom del tenant (fuera del catálogo curado).
FALLBACK_PRODUCT_PREFIX = "GEN"

#: Prefijo por categoría de producto, curado a mano — espejo EXACTO de las
#: claves de ``PRODUCT_CATEGORY_LABELS[vertical]`` (``domain/product_categories.py``)
#: menos ``"OTHER"`` (que cae al fallback ``GEN``, nunca a un prefijo propio:
#: "Otros" no es una categoría real para numerar). ``PRENDAS_SUPERIORES`` /
#: ``PRENDAS_INFERIORES`` (indumentaria) no pueden truncarse igual — de ahí
#: ``PRS``/``PRI`` en vez de dos ``PRE``. Un test recorre
#: ``PRODUCT_CATEGORY_LABELS`` completo y falla el CI si una categoría nueva
#: no tiene prefijo acá: agregar una categoría sin curarla NO cae a ``GEN`` en
#: silencio, rompe el build.
PRODUCT_CATEGORY_PREFIXES: dict[Vertical, dict[str, str]] = {
    Vertical.KIOSCO_ALMACEN: {
        "BEBIDAS": "BEB",
        "GOLOSINAS": "GOL",
        "CIGARRILLOS": "CIG",
        "ALMACEN": "ALM",
        "LACTEOS": "LAC",
        "PANIFICADOS": "PAN",
        "SNACKS": "SNA",
        "LIMPIEZA": "LIM",
        "PERFUMERIA": "PER",
        "DIARIOS_REVISTAS": "DIA",
        "REGALERIA": "REG",
    },
    Vertical.LIMPIEZA: {
        "DETERGENTES": "DET",
        "QUIMICOS": "QUI",
        "PAPEL": "PAP",
        "ACCESORIOS": "ACC",
        "AEROSOLES": "AER",
        "BOLSAS": "BOL",
    },
    Vertical.DECORACION_HOGAR: {
        "TEXTILES": "TEX",
        "ILUMINACION": "ILU",
        "MUEBLES": "MUE",
        "DECO": "DEC",
        "BAZAR": "BAZ",
        "JARDIN": "JAR",
        "AROMAS": "ARO",
    },
    Vertical.LIBRERIA_PAPELERIA: {
        "UTILES_PAPELERIA": "UTI",
        "MOCHILAS_MARROQUINERIA": "MOC",
        "LIBROS": "LIB",
        "INDUMENTARIA_ESCOLAR": "IND",
        "ARTE_DISENO": "ART",
        "REGALERIA": "REG",
        "TECNOLOGIA_OFICINA": "TEC",
    },
    Vertical.INDUMENTARIA: {
        "PRENDAS_SUPERIORES": "PRS",
        "PRENDAS_INFERIORES": "PRI",
        "ABRIGOS": "ABR",
        "CALZADO": "CAL",
        "ROPA_INTERIOR": "ROP",
        "ACCESORIOS": "ACC",
    },
    Vertical.VERDULERIA_FRUTERIA: {
        "FRUTAS": "FRU",
        "CITRICOS": "CIT",
        "CAROZO": "CAR",
        "VERDURAS_HOJA": "VER",
        "TUBERCULOS": "TUB",
        "FRUTOS_HUERTA": "FRH",
        "ENVASADOS": "ENV",
    },
}


def product_prefix_for(vertical: Vertical | None, category: str | None) -> str:
    """Prefijo de un producto: por categoría curada, o ``GEN`` si no aplica.

    ``GEN`` no es un error — es el camino honesto para: sin vertical, sin
    categoría, categoría ``"OTHER"``, o categoría custom del tenant (fuera
    del catálogo curado). Nunca lanza.
    """
    if vertical is None or category is None:
        return FALLBACK_PRODUCT_PREFIX
    return PRODUCT_CATEGORY_PREFIXES.get(vertical, {}).get(
        category, FALLBACK_PRODUCT_PREFIX
    )


def format_code(prefix: str, seq: int, width: int = 4) -> str:
    """``PREFIJO-NNNN``. Nunca trunca: un correlativo que excede ``width`` se
    escribe completo en vez de perder unicidad por padding (``GEN-12345``,
    no ``GEN-2345``)."""
    return f"{prefix}-{seq:0{width}d}"
