"""Servicio de documentación de Véktor para AgentHelper.

Carga el manual YAML y responde consultas por coincidencia de palabras clave.
No usa TF-IDF ni embeddings — matching simple para MVP.

El YAML se carga una vez en memoria (lazy). Si no existe, usa contenido mínimo.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_MANUAL_PATH = Path(__file__).parent.parent.parent.parent / "docs" / "vektor_user_manual.yaml"

# Verbos en pasado/imperativo que indican acción real de negocio (no pregunta sobre cómo usarla)
_BUSINESS_ACTION_VERBS = frozenset(
    {
        "vendí",
        "vendi",
        "cobré",
        "cobre",
        "pagué",
        "pague",
        "compré",
        "compre",
        "gasté",
        "gaste",
        "ingresé",
        "ingrese",
        "registré",
        "registre",
        "registrá",
        "registra",
        "anotá",
        "anota",
        "ajustá",
        "ajusta",
    }
)

# Palabras que indican que la pregunta es sobre cómo usar la plataforma — anulan la heurística
_PLATFORM_QUESTION_MARKERS = frozenset(
    {
        "cómo",
        "como",
        "qué",
        "que",
        "dónde",
        "donde",
        "cuándo",
        "cuando",
        "puedo",
        "puede",
        "sirve",
        "función",
        "funcion",
        "funciona",
        "ayuda",
    }
)


@dataclass
class FAQMatch:
    question: str
    answer: str
    module: str


@lru_cache(maxsize=1)
def _load_manual() -> dict[str, Any]:
    try:
        text = _MANUAL_PATH.read_text(encoding="utf-8")
        return yaml.safe_load(text) or {}
    except FileNotFoundError:
        return {}
    except yaml.YAMLError:
        return {}


# Grupos de sinónimos: tokens que el usuario usa para el mismo concepto pero que
# el manual escribe distinto. Sin esto, "¿cómo importo un Excel?" no matchea la FAQ
# "¿cómo cargo un archivo…?" porque "importo" ≠ "cargo". El matching es por keyword
# exacto, así que expandimos los tokens de la consulta a su grupo.
_SYNONYM_GROUPS: list[set[str]] = [
    {"importar", "importo", "importás", "importa", "subir", "subo", "cargar", "cargo", "cargás"},
    {"archivo", "planilla", "excel", "csv", "hoja"},
    {"borrar", "eliminar", "anular", "quitar"},
    {"editar", "modificar", "cambiar", "corregir"},
]


def _tokenize(text: str) -> set[str]:
    """Extrae palabras en minúsculas, sin puntuación."""
    return set(re.findall(r"[a-záéíóúüñ]+", text.lower()))


def _expand_synonyms(tokens: set[str]) -> set[str]:
    """Agrega a ``tokens`` los sinónimos de cada grupo que toque la consulta."""
    expanded = set(tokens)
    for group in _SYNONYM_GROUPS:
        if tokens & group:
            expanded |= group
    return expanded


def search(query: str, max_results: int = 3) -> list[FAQMatch]:
    """Busca en el manual por coincidencia de keywords. Retorna hasta `max_results` FAQs."""
    manual = _load_manual()
    modules = manual.get("modules", [])
    if not modules:
        return []

    query_tokens = _expand_synonyms(_tokenize(query))
    scored: list[tuple[int, FAQMatch]] = []

    for module in modules:
        module_name: str = module.get("name", "")
        for item in module.get("faq", []):
            q = item.get("q", "")
            a = item.get("a", "")
            q_tokens = _tokenize(q)
            a_tokens = _tokenize(a)
            score = len(query_tokens & q_tokens) * 2 + len(query_tokens & a_tokens)
            if score > 0:
                scored.append((score, FAQMatch(question=q, answer=a, module=module_name)))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [m for _, m in scored[:max_results]]


def get_sections() -> list[dict[str, Any]]:
    """Devuelve el manual como secciones navegables para el FAQ por sección del frontend.

    Cada sección: {name, slug, description, faqs: [{q, a}]}. Normaliza el whitespace
    de los scalars folded (``>``) del YAML a una sola línea.
    """
    manual = _load_manual()
    sections: list[dict[str, Any]] = []
    for module in manual.get("modules", []):
        faqs = [
            {
                "q": " ".join(str(item.get("q", "")).split()),
                "a": " ".join(str(item.get("a", "")).split()),
            }
            for item in module.get("faq", [])
            if item.get("q") and item.get("a")
        ]
        if not faqs:
            continue
        sections.append(
            {
                "name": str(module.get("name", "")).strip(),
                "slug": str(module.get("slug", "")).strip(),
                "description": " ".join(str(module.get("description", "")).split()),
                "faqs": faqs,
            }
        )
    return sections


def get_module_summary(module_slug: str) -> str | None:
    """Retorna la descripción del módulo dado su slug, o None si no existe."""
    manual = _load_manual()
    for module in manual.get("modules", []):
        if module.get("slug") == module_slug:
            return str(module.get("description", "")).strip()
    return None


def is_business_data_question(text: str) -> bool:
    """Heurística rápida: ¿este mensaje es una acción de negocio, no una pregunta de plataforma?

    Detecta verbos en pasado/imperativo ("vendí X", "registrá Y") como acción de negocio.
    Una pregunta como "¿cómo cargo una venta?" NO califica — tiene marcadores de pregunta.
    """
    tokens = _tokenize(text)
    if tokens & _PLATFORM_QUESTION_MARKERS:
        return False
    return bool(tokens & _BUSINESS_ACTION_VERBS)


def format_faq_context(matches: list[FAQMatch]) -> str:
    """Formatea los resultados como texto para incluir en el system prompt del LLM."""
    if not matches:
        return ""
    parts = ["DOCUMENTACIÓN RELEVANTE:"]
    for m in matches:
        parts.append(f"\n[{m.module}]\nP: {m.question}\nR: {m.answer.strip()}")
    return "\n".join(parts)
