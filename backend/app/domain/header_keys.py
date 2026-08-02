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

# Preposiciones y artículos que no aportan al matching.
STOPWORDS: frozenset[str] = frozenset({"de", "del", "la", "el", "los", "las", "por"})


def match_key(normalized: str) -> str:
    """El header ya normalizado, sin preposiciones.

    Espera la salida de una normalización previa (lowercase + underscores), no un
    header crudo.
    """
    parts = [p for p in normalized.split("_") if p and p not in STOPWORDS]
    # Un header que sea SOLO stopwords ("de") dejaría la clave vacía; se devuelve
    # el original antes que una cadena vacía.
    return "_".join(parts) or normalized
