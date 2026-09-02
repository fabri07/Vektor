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
#: Vocabulario por vertical. Se amplía SOLO contra nombres reales medidos, y una
#: palabra entra únicamente si dentro de este catálogo no admite dos lecturas:
#: una categoría equivocada es peor que ninguna (regla de no-invención), porque
#: el usuario no tiene cómo saber que la que ve está mal.
#:
#: Deliberadamente AFUERA, aunque son 32 productos reales sin categoría:
#: `canasto`, `cesto`, `organizador` y la familia `porta*` (porta bolsas, porta
#: cepillo, porta utensilios). Son artículos de ORGANIZACIÓN y este catálogo no
#: tiene esa categoría: meterlos en DECO o en BAZAR sería elegir por el negocio.
#: Es un hueco del catálogo del rubro, no del vocabulario, y se decide aparte.
CATEGORY_KEYWORDS: dict[Vertical, dict[str, tuple[str, ...]]] = {
    Vertical.DECORACION_HOGAR: {
        "TEXTILES": (
            "textil", "tela", "cortina", "almohadon", "manta", "sabana",
            "acolchado", "funda", "mantel", "cubrecama",
            # Medidos contra los 398 productos reales de un cliente del rubro:
            # ninguno de estos matcheaba nada. "alfombras" ya era TEXTILES en el
            # mapa de alias de `product_categories`, así que la inferencia estaba
            # diciendo algo distinto de lo que el mismo dominio ya afirmaba.
            "alfombra", "frazada", "repasador",
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
            # Utensilios y recipientes de cocina/mesa, todos verificados uno por
            # uno contra los nombres reales antes de agregarlos ("huevera x 6
            # hoyos", "especiero apilable granito", "tabla de picar pino"): son
            # los que no matcheaban ningún keyword y no admiten otra lectura
            # dentro de este catálogo.
            "bandeja", "frasco", "huevera", "aceitero", "especiero", "salero",
            "batidor", "espatula", "utensilio", "hermetico", "molde",
            "medidora", "cafetera", "tabla",
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


#: Palabras que convierten el nombre en "un aparato PARA X" en vez de en X.
#: No invalidan cualquier match: un `portavela` se vende con las velas (AROMAS),
#: un `posavasos` y un `escurridor de cubiertos` son artículos de mesa y cocina
#: (BAZAR). El soporte pertenece a la misma familia que lo que sostiene.
_SOPORTES = (
    "porta", "cuelga", "posa", "apoya", "soporte", "colgador", "sujeta",
    "organizador", "escurridor", "percha",
)

#: Categorías donde un soporte SÍ invalida el match, porque ahí las palabras
#: nombran un material o una prenda y el aparato que las sostiene está hecho de
#: otra cosa: un "porta repasadores" es un herraje, no un textil, igual que un
#: "organizador de tela" es un organizador y no una tela. BAZAR y AROMAS no
#: están porque ahí el soporte no cambia de familia.
#:
#: El soporte tampoco cae en otra categoría: es un artículo de ORGANIZACIÓN, y
#: este catálogo no tiene esa categoría (misma razón por la que `canasto`,
#: `cesto` y la familia `porta*` están fuera del vocabulario). Sin categoría es
#: la respuesta correcta, no una respuesta incompleta.
_SOPORTE_INVALIDA: frozenset[str] = frozenset({"TEXTILES"})


def _es_soporte_de_otra_cosa(code: str, text: str) -> bool:
    return code in _SOPORTE_INVALIDA and any(s in text for s in _SOPORTES)


def infer_category(
    vertical: Vertical,
    name: str | None,
    specifications: str | None = None,
) -> CategorySuggestion:
    """Sugiere una categoría del catálogo canónico del vertical, o ninguna.

    Nunca inventa un código fuera de `CATEGORY_KEYWORDS[vertical]`. Sin
    catálogo para el vertical (todavía no cubierto), siempre `low`/`None`.

    Un soporte no hereda la categoría de lo que sostiene cuando las dos cosas
    son de familias distintas (ver `_SOPORTE_INVALIDA`): "porta repasadores" no
    es un textil. Preferimos no categorizar antes que categorizar mal — una
    categoría equivocada con confianza alta se aplica sola y el usuario no tiene
    cómo saber que está mal, mientras que "sin categoría" se ve y se corrige.
    """
    catalog = CATEGORY_KEYWORDS.get(vertical)
    if not catalog:
        return _NO_SUGGESTION

    name_norm = _norm(name) if name else ""
    name_hits = _matches(catalog, name_norm) if name else []
    if len(name_hits) == 1:
        code, kw = name_hits[0]
        if _es_soporte_de_otra_cosa(code, name_norm):
            return _NO_SUGGESTION
        return CategorySuggestion(code=code, confidence="high", matched_text=kw, rule=f"name:{kw}")

    # Nombre ambiguo (0 o ≥2 candidatas): las especificaciones pueden desempatar.
    spec_hits = _matches(catalog, _norm(specifications)) if specifications else []
    if name_hits:
        candidate_codes = {code for code, _ in name_hits}
        spec_hits = [h for h in spec_hits if h[0] in candidate_codes]
    if len(spec_hits) == 1:
        code, kw = spec_hits[0]
        # El soporte lo declara el NOMBRE, no las especificaciones: desempatar
        # con la ficha técnica no convierte un herraje en un textil.
        if _es_soporte_de_otra_cosa(code, name_norm):
            return _NO_SUGGESTION
        return CategorySuggestion(
            code=code, confidence="medium", matched_text=kw, rule=f"specifications:{kw}"
        )

    return _NO_SUGGESTION
