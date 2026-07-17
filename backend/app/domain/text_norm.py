"""Normalización canónica de texto para matching (fuente única de verdad).

``normalize_text`` es la implementación de referencia para comparar texto libre
nuevo (nombres de proveedor vs marca, etiquetas, etc.): NFKD + quitar diacríticos
+ casefold + trim + colapso de espacios internos. Antes de escribir otra copia de
esta cadena, delegá acá.

NO migrar ``inventory_movement_origin._norm_text`` a este helper: alimenta
``compute_source_row_hash`` y cambiar la normalización cambiaría los hashes,
rompiendo la idempotencia de la relectura (reread). Solo podría converger con una
migración de hashes ya persistidos.
"""

from __future__ import annotations

import re
import unicodedata


def normalize_text(s: str) -> str:
    """NFKD + quita diacríticos + casefold + trim + colapsa espacios internos."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.casefold().split())


def normalize_barcode(s: str | None) -> str | None:
    """Código de barras normalizado: SOLO dígitos. None/vacío/sin dígitos → None."""
    if not s:
        return None
    digits = re.sub(r"\D", "", s)
    return digits or None


def normalize_sku(s: str | None) -> str | None:
    """SKU normalizado (``normalize_text``). None/vacío → None."""
    if not s:
        return None
    return normalize_text(s) or None


def normalize_product_name(s: str | None) -> str:
    """Nombre de producto normalizado: colapsa ``[-_]`` → espacio y luego
    ``normalize_text`` (superset para matching: 'Coca-Cola' == 'Coca Cola' ==
    'coca_cola'). '' si vacío.
    """
    if not s:
        return ""
    return normalize_text(re.sub(r"[-_]+", " ", s))


def normalize_brand(s: str | None) -> str | None:
    """Marca normalizada (``normalize_text``). None/vacío → None."""
    if not s:
        return None
    return normalize_text(s) or None
