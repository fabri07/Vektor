"""Parser robusto del JSON que devuelven los LLMs.

Los modelos a veces envuelven el JSON en fences markdown (```json … ```) o le
agregan texto antes/después, lo que rompe ``json.loads`` plano. Este helper
centraliza la tolerancia a esos casos para que TODOS los agentes parseen igual
(en vez de duplicar regex frágiles por agente).
"""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def parse_llm_json(raw: str | None) -> dict[str, Any] | None:
    """Devuelve el objeto JSON del texto del LLM, o ``None`` si no se pudo parsear.

    Estrategia, en orden:
      1. ``json.loads`` directo del texto pelado.
      2. Quita fences markdown (```` ```json ```` / ```` ``` ````) y reintenta.
      3. Extrae el primer objeto ``{ … }`` balanceado del texto y lo parsea.

    Solo acepta objetos (dict); listas u otros escalares devuelven ``None``.
    """
    if not raw:
        return None
    text = raw.strip()

    for candidate in (text, _strip_fences(text), _extract_object(text)):
        if candidate is None:
            continue
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _strip_fences(text: str) -> str:
    """Quita un bloque de código markdown que envuelva todo el texto."""
    stripped = _FENCE_RE.sub("", text).strip()
    return stripped


def _extract_object(text: str) -> str | None:
    """Extrae el primer objeto ``{ … }`` balanceado (respeta strings y escapes)."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None
