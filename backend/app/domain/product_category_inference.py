"""Bloque 3B — sugerencia de categoría de producto desde nombre + especificaciones.

Pura, sin DB, sin heurística de LLM. Solo se usa cuando el archivo NO trae una
columna de categoría explícita (`cat_raw` vacío en `_add_product`) — nunca
reemplaza una categoría que el propio archivo declara, y nunca inventa un
código fuera del catálogo canónico del vertical.

Confianza en tres niveles (criterio del usuario, no inventado acá):
- ``high``  — el NOMBRE solo matchea una única categoría, sin ambigüedad.
  Se aplica (`product.category`), visible y revisable.
- ``medium`` — el nombre es ambiguo (0 o ≥2 categorías candidatas) y las
  ESPECIFICACIONES lo desempatan a una sola. Queda como sugerencia, no se
  aplica directo — la confirmación es responsabilidad del caller (Bloque 5).
- ``low``   — no hay evidencia suficiente. No se asigna categoría: no-invención.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from app.domain.expense_categories import strip_accents
from app.domain.verticals import Vertical

Confidence = Literal["high", "medium", "low"]

#: Keywords por categoría, por vertical. Un match de CUALQUIER keyword de la
#: tupla alcanza como evidencia para esa categoría — no hace falta match exacto
#: de frase. Catálogo cerrado: los códigos son los mismos que ya usa el
#: catálogo canónico de producto del vertical (nunca se inventa uno nuevo acá).
#:
#: Deliberadamente NO reusa `product_categories._ALIASES` (que alimenta
#: `normalize_product_category`): esos alias están en plural, pensados para
#: normalizar el VALOR de una columna "categoría" ya declarada por el usuario
#: (ej. "Sillas"). Acá se matchea contra el NOMBRE del producto, casi siempre
#: singular ("Silla de living") — con match por substring, un keyword singular
#: ya cubre el plural regular ("silla" ⊂ "sillas"), pero uno plural no cubre el
#: singular. Reusar el alias de categoría habría perdido la mayoría de los
#: nombres reales.
CATEGORY_KEYWORDS: dict[Vertical, dict[str, tuple[str, ...]]] = {
    Vertical.DECORACION_HOGAR: {
        "TEXTILES": (
            "textil", "tela", "cortina", "almohadon", "manta", "sabana",
            "acolchado", "funda", "mantel", "cubrecama",
        ),
        "ILUMINACION": (
            "lampara", "luz", "velador", "candelabro", "aplique",
            "iluminacion", "farol", "plafon",
        ),
        "MUEBLES": (
            "silla", "mesa", "sillon", "estante", "mueble", "ratona",
            "escritorio", "banco", "repisa", "modular", "aparador",
        ),
        "DECO": (
            "cuadro", "florero", "adorno", "decoracion", "espejo",
            "portarretrato", "figura", "jarron",
        ),
        "BAZAR": (
            "vajilla", "taza", "plato", "cocina", "cubierto", "fuente",
            "bowl", "copa", "vaso",
        ),
        "JARDIN": (
            "maceta", "jardin", "exterior", "regadera", "planta", "reja",
        ),
        "AROMAS": (
            "vela", "aroma", "sahumerio", "difusor", "esencia", "incienso",
        ),
    },
}


@dataclass(frozen=True)
class CategorySuggestion:
    code: str | None
    confidence: Confidence
    #: Texto/keyword que motivó la sugerencia — evidencia para revisión humana.
    matched_text: str | None
    #: Qué campo/regla decidió: "name:{keyword}" o "specifications:{keyword}".
    rule: str | None


_NO_SUGGESTION = CategorySuggestion(code=None, confidence="low", matched_text=None, rule=None)


def _norm(text: str) -> str:
    stripped = strip_accents(text.strip().lower())
    return re.sub(r"[\s\-_/]+", " ", stripped)


def _matches(catalog: dict[str, tuple[str, ...]], text: str) -> list[tuple[str, str]]:
    """Categorías cuyo alias aparece en `text`, con el alias que matcheó.

    Una sola evidencia por categoría alcanza (no se acumulan puntajes) — el
    criterio es "¿aparece o no?", no un scoring por cantidad de keywords.
    """
    hits: list[tuple[str, str]] = []
    if not text:
        return hits
    for code, keywords in catalog.items():
        for kw in keywords:
            if kw in text:
                hits.append((code, kw))
                break
    return hits


def infer_category(
    vertical: Vertical,
    name: str | None,
    specifications: str | None = None,
) -> CategorySuggestion:
    """Sugiere una categoría del catálogo canónico del vertical, o ninguna.

    Nunca inventa un código fuera de `CATEGORY_KEYWORDS[vertical]`. Sin
    catálogo para el vertical (todavía no cubierto), siempre `low`/`None`.
    """
    catalog = CATEGORY_KEYWORDS.get(vertical)
    if not catalog:
        return _NO_SUGGESTION

    name_hits = _matches(catalog, _norm(name)) if name else []
    if len(name_hits) == 1:
        code, kw = name_hits[0]
        return CategorySuggestion(code=code, confidence="high", matched_text=kw, rule=f"name:{kw}")

    # Nombre ambiguo (0 o ≥2 candidatas): las especificaciones pueden desempatar.
    spec_hits = _matches(catalog, _norm(specifications)) if specifications else []
    if name_hits:
        candidate_codes = {code for code, _ in name_hits}
        spec_hits = [h for h in spec_hits if h[0] in candidate_codes]
    if len(spec_hits) == 1:
        code, kw = spec_hits[0]
        return CategorySuggestion(
            code=code, confidence="medium", matched_text=kw, rule=f"specifications:{kw}"
        )

    return _NO_SUGGESTION
