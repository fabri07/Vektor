"""Clave de matching heurístico para headers de planillas.

Función pura, sin dependencias: la comparten el mapeo de columnas
(``column_mapping_service``) y el clasificador de hojas (``file_parsing``), que
tienen el mismo problema — "Precio de compra" y "Precio compra" son el mismo
header para una heurística, y declarar las dos variantes en cada set de keywords
es inmantenible.

Lo que NO hay que hacer con esto: aplicarlo a un valor que se persista. La forma
normalizada que se guarda en ``tenant_column_mappings.source_column`` es la de
``_normalize_col``; colapsar preposiciones ahí dejaría huérfano el historial de
alias aprendidos por cada tenant.
"""

from __future__ import annotations

import re

from app.domain.text_norm import normalize_text

# Preposiciones y artículos que no aportan al matching.
STOPWORDS: frozenset[str] = frozenset({"de", "del", "la", "el", "los", "las", "por"})

#: Tope de la clave. La columna es ``String(80)``; se corta antes para dejar
#: lugar al sufijo de desambiguación (``_2``, ``_10``) sin volver a pasarse.
_SLUG_MAX = 72

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def match_key(normalized: str) -> str:
    """El header ya normalizado, sin preposiciones.

    Espera la salida de una normalización previa (lowercase + underscores), no un
    header crudo.
    """
    parts = [p for p in normalized.split("_") if p and p not in STOPWORDS]
    # Un header que sea SOLO stopwords ("de") dejaría la clave vacía; se devuelve
    # el original antes que una cadena vacía.
    return "_".join(parts) or normalized


def custom_field_slug(header: str | None) -> str | None:
    """Encabezado crudo → clave de campo propio, o ``None`` si no queda nada.

    F-A. Es un IDENTIFICADOR que se persiste en
    ``tenant_custom_field_definitions.field_key`` (``String(80)``), no una clave
    de matching: **no confundir con `_normalize_col`**, que alimenta
    ``tenant_column_mappings.source_column`` y no se puede tocar sin migrar los
    alias aprendidos por cada tenant. `_normalize_col` además no sirve acá —
    deja acentos y puntos (``"P. Venta"`` → ``"p._venta"``).

    La forma que produce es la que ``POST /fields`` ya exige a mano
    (``^[a-z][a-z0-9_]*$``, 2–80). Hasta acá la ingesta no validaba nada y podía
    escribir claves que su propia API rechazaba.

    Devuelve ``None`` —y no ``""``— cuando el encabezado no aporta ni un
    carácter usable (vacío, sólo espacios, sólo puntuación): el caller tiene que
    decidir qué hacer con una columna sin nombre, no recibir una clave vacía que
    después colisione con la de al lado.
    """
    if header is None:
        return None
    # Acentos por el normalizador canónico del proyecto: escribir otra cadena
    # NFKD acá es justo lo que `text_norm` pide no hacer.
    base = normalize_text(header)
    slug = _NON_ALNUM.sub("_", base).strip("_")
    if not slug:
        return None
    # Un identificador no puede arrancar con dígito ("2024" → "c_2024") ni medir
    # menos de dos caracteres ("A" → "c_a"): son las dos reglas que el schema de
    # `/fields` ya exige (`^[a-z][a-z0-9_]*$`, min_length=2). El mismo prefijo
    # resuelve las dos, así que una columna "1" y una columna "A" quedan igual de
    # válidas sin inventar un segundo mecanismo.
    if slug[0].isdigit() or len(slug) < 2:
        slug = f"c_{slug}"
    # Cortar por el tope y volver a limpiar: truncar puede dejar un `_` colgando.
    return slug[:_SLUG_MAX].rstrip("_") or None
