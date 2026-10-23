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
from functools import lru_cache


@lru_cache(maxsize=4096)
def repair_mojibake(s: str) -> str:
    """Repara texto doble-codificado: ``MercaderÃ­a`` → ``Mercadería``.

    Es el caso que NINGUNA escalera de decodificación puede arreglar: el archivo
    ya venía roto de fábrica (alguien abrió un CSV UTF-8 como cp1252 y lo volvió
    a guardar), así que sus bytes son UTF-8 *válido* y decodifican sin error — a
    un texto que no es el que el usuario escribió. Los acentos y la ñ son
    justamente los caracteres que sobreviven mal a ese ida y vuelta.

    Usa ``ftfy.fix_encoding``, NO ``fix_text``: sólo deshace la doble
    codificación y deja el resto en paz (``fix_text`` además toca comillas
    tipográficas, ligaduras y espacios, que acá no hay motivo para cambiar).
    Sobre texto sano es identidad.

    Import diferido y degradación silenciosa, igual que ``rapidfuzz`` en el
    mapeo: si ftfy no está instalado el texto pasa sin reparar y el pipeline
    sigue funcionando como antes, en vez de tumbar el import de un módulo de
    dominio.
    """
    if not s:
        return s
    try:
        from ftfy import fix_encoding  # noqa: PLC0415
    except ImportError:
        return s
    return fix_encoding(s)


def normalize_text(s: str) -> str:
    """NFKD + quita diacríticos + casefold + trim + colapsa espacios internos.

    NO repara mojibake a propósito: alimenta ``product_name_normalized`` y demás
    claves de identidad ya persistidas, y cambiar lo que devuelve para un texto
    dado movería filas que ya están en la base. La reparación vive en
    ``repair_mojibake``, que se aplica sólo a encabezados (``fold_header``).
    """
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.casefold().split())


# Rango de longitudes plausibles para un código de barras retail: EAN-8 (8),
# UPC-A (12), EAN-13 (13), ITF-14/GTIN-14 (14). Se admite todo el rango 8–14
# para no rechazar códigos internos cortos legítimos.
_BARCODE_MIN_DIGITS = 8
_BARCODE_MAX_DIGITS = 14


def normalize_barcode(s: str | None) -> str | None:
    """Código de barras normalizado: SOLO dígitos, y solo si el resultado tiene
    una longitud PLAUSIBLE de barcode (8–14 dígitos). None/vacío → None.

    El gate de longitud evita colisiones falsas: un SKU o nombre con dígitos
    embebidos (``"producto-123"`` → ``"123"``, ``"ABC123"`` → ``"123"``) NO es
    un código de barras y debe caer a None, no fusionarse con otros productos
    por esos 3 dígitos. El campo raw se preserva aparte (columna ``barcode``);
    esto normaliza solo la clave de matching.
    """
    if not s:
        return None
    digits = re.sub(r"\D", "", s)
    if not (_BARCODE_MIN_DIGITS <= len(digits) <= _BARCODE_MAX_DIGITS):
        return None
    return digits


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
