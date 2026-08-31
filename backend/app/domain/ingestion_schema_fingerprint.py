"""Bloque 5 — huellas de esquema para persistir decisiones de mapeo por tenant.

Dos hashes, no uno, porque cubren preguntas distintas:

- ``schema_fingerprint``: "¿este archivo tiene la MISMA forma general que otro
  que ya vimos?" — depende del tipo de archivo y de las columnas normalizadas
  de TODOS los contextos, nunca del ``file_id`` ni del nombre de la hoja.
- ``context_signature``: "¿esta hoja puntual, dentro de ese archivo, es la
  MISMA hoja que ya vimos?" — depende de las columnas normalizadas de ESA
  hoja + la entidad detectada (dos hojas con las mismas columnas pero
  entidades distintas no son el mismo contexto).

Ambos son insensibles al ORDEN de las columnas (se ordenan antes de hashear) —
así una relectura con las columnas reordenadas sigue matcheando — pero
sensibles a que el SET de columnas cambie: agregar o sacar una columna
cambia el hash a propósito (una decisión vieja no debe aplicarse en silencio
sobre un esquema distinto).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any


def normalize_column_name(raw: str) -> str:
    """lower, sin tildes, separadores colapsados — para que "Precio de Compra"
    y "precio_de_compra" hasheen igual."""
    stripped = "".join(
        c for c in unicodedata.normalize("NFKD", str(raw)) if not unicodedata.combining(c)
    )
    return re.sub(r"[\s\-_/]+", "_", stripped.strip().lower())


def _hash(canonical: str) -> str:
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_schema_fingerprint(file_type: str, contexts: list[dict[str, Any]]) -> str:
    """Huella del ARCHIVO entero: tipo + columnas normalizadas de todos los
    contextos (unión, sin duplicados), sin importar en qué hoja vive cada una
    ni el orden de las hojas."""
    all_cols: set[str] = set()
    for ctx in contexts:
        for header in ctx.get("headers") or []:
            norm = normalize_column_name(header)
            if norm:
                all_cols.add(norm)
    canonical = f"{file_type}|" + "|".join(sorted(all_cols))
    return _hash(canonical)


def compute_context_signature(context: dict[str, Any]) -> str:
    """Huella de UNA hoja/contexto: entidad detectada + sus columnas
    normalizadas, ordenadas. Agregar/sacar una columna cambia la huella;
    reordenarlas no."""
    headers = sorted(
        {normalize_column_name(h) for h in (context.get("headers") or []) if h}
    )
    entity = str(context.get("entity_type") or "")
    canonical = f"{entity}|" + "|".join(headers)
    return _hash(canonical)
