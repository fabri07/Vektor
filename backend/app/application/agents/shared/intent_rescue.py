"""IntentRescue — rescate determinístico de intents ambiguos (Sprint 17, Stage 0.5).

Capa COMPLEMENTARIA al AgentCEO. Se ejecuta SOLO cuando el CEO devuelve
`intent_desconocido`. No reemplaza al CEO: actúa como segunda línea de defensa
para que Véktor no corte con "fuera de scope" cuando el usuario habla como dueño
de negocio aunque use frases imprecisas, coloquiales o con errores de tipeo.

Principio: clasificar por combinación de **verbo ambiguo + objeto de negocio +
contexto disponible** (attachment y su tipo), no solo por intent explícito.

Decisión de diseño (ajuste #1): se CONSERVA `out_of_scope`. Si el mensaje no tiene
ninguna señal de negocio (ni verbo ambiguo ni objeto), devuelve `out_of_scope` y el
ChatOrchestrator corta normalmente. Mensajes que suenan a negocio pero son
imprecisos → piden aclaración, nunca se cortan.

Sin LLM. Sin estado. Determinístico y barato.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

try:
    from rapidfuzz import fuzz
except ModuleNotFoundError:

    class _FallbackFuzz:
        @staticmethod
        def ratio(left: str, right: str) -> int:
            return int(SequenceMatcher(None, left, right).ratio() * 100)

    fuzz = _FallbackFuzz()

# Umbral de similitud para fuzzy matching (tolera typos: "presios", "stok", "catologo")
_FUZZ_THRESHOLD = 85

# ── Normalización de voseo / imperativos rioplatenses (ajuste #4) ─────────────
# Se aplica ANTES de quitar tildes para mapear la forma acentuada a la base.
_VOSEO_MAP = {
    "analizame": "analiza",
    "analizá": "analiza",
    "analiza": "analiza",
    "importá": "importa",
    "importalo": "importa",
    "importala": "importa",
    "cargá": "carga",
    "cargalo": "carga",
    "cargala": "carga",
    "anotá": "anota",
    "registrá": "registra",
    "subí": "subir",
    "guardá": "guarda",
    "ingresá": "ingresa",
    "mirame": "mira",
    "mirá": "mira",
    "miralo": "mira",
    "mirala": "mira",
    "chequeame": "chequea",
    "chequeá": "chequea",
    "chequealo": "chequea",
    "revisame": "revisa",
    "revisá": "revisa",
    "revisalo": "revisa",
    "revisala": "revisa",
    "fijate": "fija",
    "resumime": "resumi",
    "resumí": "resumi",
    "resume": "resumi",
    "ordename": "ordena",
    "ordená": "ordena",
    "limpiame": "limpia",
    "limpiá": "limpia",
    "normalizame": "normaliza",
    "normalizá": "normaliza",
    "prolijame": "prolija",
    "prolijá": "prolija",
    "comparame": "compara",
    "compará": "compara",
    "simulame": "simula",
    "simulá": "simula",
    "estimame": "estima",
    "estimá": "estima",
    "proyectame": "proyecta",
    "proyectá": "proyecta",
    "decime": "decir",
    "contame": "contar",
    "armame": "armar",
    "sugerime": "sugiere",
    "sugerí": "sugiere",
    "priorizame": "prioriza",
    "priorizá": "prioriza",
}


def normalize(text: str) -> str:
    """Normalización OBLIGATORIA previa al fuzzy matching (ajuste #4).

    Pasos: lowercase → voseo/imperativos → quitar tildes → limpiar signos →
    colapsar espacios. Baja falsos negativos antes de que `partial_ratio` trabaje.
    """
    # 1. lowercase
    text = text.lower()
    # 2. normalizar voseo/imperativos frecuentes (antes de quitar tildes)
    for voseo, base in _VOSEO_MAP.items():
        text = re.sub(rf"\b{re.escape(voseo)}\b", base, text)
    # 3. quitar tildes
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    # 4. limpiar signos de puntuación (conservar espacios y alfanuméricos)
    text = re.sub(r"[^\w\s]", " ", text)
    # 5. colapsar espacios
    return re.sub(r"\s+", " ", text).strip()


# ── Diccionario de verbos ambiguos (ya normalizados, sin tildes) ──────────────
VERBS_ANALIZAR = frozenset(
    {
        "analiza",
        "mira",
        "revisa",
        "chequea",
        "fija",
        "pegale una mirada",
        "decime que ves",
        "que ves",
        "que hay",
        "que onda",
        "ver",
    }
)
VERBS_RESUMIR = frozenset(
    {
        "resumi",
        "dame un resumen",
        "sacame conclusiones",
        "que hay aca",
        "diagnostico",
    }
)
VERBS_VALIDAR = frozenset(
    {
        "esta bien",
        "tiene sentido",
        "hay errores",
        "detecta problemas",
        "esta ok",
        "esta bien cargado",
        "bien cargado",
    }
)
VERBS_LIMPIAR = frozenset(
    {
        "ordena",
        "limpia",
        "acomoda",
        "normaliza",
        "prolija",
        "desordenado",
    }
)
VERBS_COMPARAR = frozenset(
    {
        "compara",
        "contra la anterior",
        "que cambio",
        "cuanto aumento",
        "diferencia",
    }
)
VERBS_PROYECTAR = frozenset(
    {
        "que pasa si",
        "simula",
        "estima",
        "proyecta",
        "si vendo menos",
        "si subo",
    }
)
VERBS_NEGOCIO = frozenset(
    {
        "como vengo",
        "como esta el negocio",
        "como viene el negocio",
        "como viene",
        "como vamos",
        "como va el negocio",
        "me conviene",
        "donde pierdo",
        "cuanto gano",
    }
)
VERBS_IMPORTAR = frozenset(
    {
        "importa",
        "importar",
        "carga",
        "cargar",
        "subir",
        "subi",
        "anota",
        "anotar",
        "registra",
        "registrar",
        "guarda",
        "guardar",
        "ingresa",
        "ingresar",
    }
)

# Verbos que disparan análisis genérico (cualquiera de estos sets salvo NEGOCIO)
_AMBIGUOUS_VERB_SETS = (
    VERBS_ANALIZAR,
    VERBS_RESUMIR,
    VERBS_VALIDAR,
    VERBS_LIMPIAR,
    VERBS_COMPARAR,
    VERBS_PROYECTAR,
)

# ── Diccionario de objetos de negocio (normalizados) ──────────────────────────
OBJECTS_PRECIOS = frozenset(
    {
        "lista",
        "lista de precios",
        "catalogo",
        "productos",
        "articulos",
        "sku",
        "menu",
        "tarifario",
        "precios",
        "precio",
    }
)
OBJECTS_STOCK = frozenset(
    {
        "inventario",
        "mercaderia",
        "existencias",
        "unidades",
        "reponer",
        "faltantes",
        "quiebres",
        "stock",
        "mercaderias",
    }
)
OBJECTS_VENTAS = frozenset(
    {
        "facturacion",
        "ingresos",
        "cobros",
        "tickets",
        "operaciones",
        "pedidos",
        "ventas",
        "venta",
    }
)
OBJECTS_GASTOS = frozenset(
    {
        "egresos",
        "costos",
        "pagos",
        "compras",
        "salidas",
        "gastos",
        "gasto",
    }
)
OBJECTS_CAJA = frozenset(
    {
        "plata",
        "saldo",
        "liquidez",
        "cashflow",
        "flujo",
        "cuanto me queda",
        "efectivo",
        "caja",
    }
)
OBJECTS_MARGEN = frozenset(
    {
        "ganancia",
        "rentabilidad",
        "markup",
        "lo que me deja",
        "cuanto gano",
        "margen",
    }
)
OBJECTS_PROVEEDOR = frozenset(
    {
        "proveedor",
        "proveedores",
        "remarcar",
        "remarcacion",
        "aumento",
        "aumentos",
    }
)


def _contains_any(normalized: str, terms: frozenset[str]) -> bool:
    """True si algún término aparece en el mensaje (substring exacto o fuzzy)."""
    tokens = normalized.split()
    for term in terms:
        if " " in term:
            # frase multi-palabra → substring directo
            if term in normalized:
                return True
        else:
            # palabra suelta → exacta o fuzzy contra cada token (tolera typos)
            if term in tokens:
                return True
            for tok in tokens:
                if len(tok) >= 4 and fuzz.ratio(term, tok) >= _FUZZ_THRESHOLD:
                    return True
    return False


def _has_ambiguous_verb(normalized: str) -> bool:
    return any(_contains_any(normalized, vs) for vs in _AMBIGUOUS_VERB_SETS)


def _has_import_verb(normalized: str) -> bool:
    return _contains_any(normalized, VERBS_IMPORTAR)


def _import_intent_for_attachment_type(attachment_type: str | None) -> str:
    if attachment_type == "product":
        return "importar_archivo_productos"
    if attachment_type == "expense":
        return "importar_archivo_gastos"
    return "importar_archivo_ventas"


# ── Resultado del rescate ─────────────────────────────────────────────────────
# Devuelve (intent, entities) donde intent es del INTENT_CATALOG consolidado (o un
# sentinela de aclaración / "out_of_scope") y entities lleva `analysis_type` cuando
# el sub-análisis es específico. Sprint 19: el rescate emite intents consolidados,
# no los granulares viejos.


def rescue_intent(
    message: str,
    has_attachment: bool,
    attachment_type: str | None = None,
) -> tuple[str, dict[str, str]]:
    """Rescata un intent a partir de un mensaje que el CEO no pudo clasificar.

    Args:
        message: texto crudo del usuario.
        has_attachment: True si hay al menos un archivo adjunto.
        attachment_type: tipo detectado del adjunto ("product"/"stock"/"sale"/
            "expense"/"mixed"/None) — proviene de DataIntentExtractor.

    Returns:
        Tupla (intent, entities): un intent consolidado del catálogo + entities con
        `analysis_type` opcional, o ("pedir_aclaracion_sobre_archivo", {}) /
        ("pedir_aclaracion_negocio", {}) / ("out_of_scope", {}).
    """
    norm = normalize(message)
    verb = _has_ambiguous_verb(norm)
    import_verb = _has_import_verb(norm)

    # ── Reglas con attachment (prioridad alta) ────────────────────────────────
    if has_attachment:
        if import_verb:
            return _import_intent_for_attachment_type(attachment_type), {}
        # 10. "que puedo hacer con esto" + attachment
        if "que puedo hacer" in norm or "que podes hacer" in norm:
            return "ayuda_plataforma", {"analysis_type": "explicar_datos"}
        # 1. attachment de productos/precios: verbo analítico → análisis; sin verbo → importar
        if attachment_type == "product":
            if verb:
                return "analizar_precios", {"analysis_type": "lista"}
            return "importar_archivo_productos", {}
        # 2. attachment de stock: verbo analítico → análisis; sin verbo → importar catálogo
        if attachment_type == "stock":
            if verb:
                return "analizar_stock", {}
            return "importar_archivo_productos", {}
        # 3. attachment de ventas/gastos/mixto: verbo analítico → análisis; sin verbo → importar
        if attachment_type == "expense":
            if verb:
                return "analizar_archivo", {}
            return "importar_archivo_gastos", {}
        if attachment_type in ("sale", "mixed"):
            if verb:
                return "analizar_archivo", {}
            return "importar_archivo_ventas", {}
        # 4. attachment sin tipo claro + verbo ambiguo → análisis genérico
        if verb:
            return "analizar_archivo", {}
        # 11. attachment sin tipo claro y sin señal → análisis genérico (mejor que preguntar)
        return "analizar_archivo", {}

    # ── Reglas sin attachment: objeto de negocio detectado ────────────────────
    # 5. márgenes / rentabilidad
    if _contains_any(norm, OBJECTS_MARGEN):
        return "analizar_precios", {"analysis_type": "margenes"}
    # 6. proveedor + aumento/remarcar
    if _contains_any(norm, OBJECTS_PROVEEDOR):
        if "remarcar" in norm or "remarcacion" in norm:
            return "analizar_precios", {"analysis_type": "simulacion"}
        if "aumento" in norm or "aumentos" in norm:
            return "analizar_precios", {"analysis_type": "aumentos_proveedor"}
        return "analizar_proveedores", {}
    # 7. caja / liquidez
    if _contains_any(norm, OBJECTS_CAJA):
        return "proyectar_caja", {}
    # 8. stock
    if _contains_any(norm, OBJECTS_STOCK):
        return "analizar_stock", {"analysis_type": "quiebres"}
    # 9. señales de "cómo viene el negocio"
    if _contains_any(norm, VERBS_NEGOCIO):
        return "consultar_estado_negocio", {}
    # precios / catálogo sin attachment
    if _contains_any(norm, OBJECTS_PRECIOS):
        return "analizar_precios", {}
    # gastos
    if _contains_any(norm, OBJECTS_GASTOS):
        return "analizar_gastos", {"analysis_type": "clasificacion"}
    # ventas
    if _contains_any(norm, OBJECTS_VENTAS):
        return "analizar_ventas", {"analysis_type": "rentabilidad"}

    # ── 12. Verbo ambiguo pero ningún objeto claro → pedir aclaración ─────────
    if verb:
        return "pedir_aclaracion_negocio", {}

    # ── 13. Ninguna señal de negocio → out_of_scope (ajuste #1) ──────────────
    return "out_of_scope", {}
