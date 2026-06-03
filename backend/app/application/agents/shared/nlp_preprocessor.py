"""NLP Preprocessor — normalización de mensajes en español rioplatense.

Responsabilidades:
1. Normalizar lunfardo, voseo y jerga argentina → español estándar
2. Usar spacy (es_core_news_sm) para lematización y extracción de entidades
3. Retornar mensaje normalizado + entidades pre-extraídas para el CEO

Uso:
    from app.application.agents.shared.nlp_preprocessor import preprocess
    result = preprocess("vendí 3 facturas de jabón a 500 mangos")
    # result.normalized_message → "vendí 3 facturas de jabón a 500 pesos"
    # result.entities → {"amount_hint": "500", "action_hint": "vender"}
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# ── Diccionario de lunfardo/rioplatense → español estándar ──────────────────
# Solo términos de negocio relevantes para Véktor
_SLANG: dict[str, str] = {
    # Dinero
    "mango": "peso",
    "mangos": "pesos",
    "guita": "dinero",
    "luca": "mil pesos",
    "lucas": "mil pesos",
    "palo": "mil pesos",
    "palos": "mil pesos",
    "biyuya": "dinero",
    "plata": "dinero",
    # Negocio / comercio
    "merca": "mercadería",
    "laburo": "trabajo",
    "laburar": "trabajar",
    "laburé": "trabajé",
    "curro": "negocio",
    "fiado": "crédito",
    "al fiado": "a crédito",
    "a cuenta": "a crédito",
    # Verbos comunes en voseo (normalizar a forma infinitivo/yo)
    "anotame": "registrá",
    "cargame": "cargá",
    "meteme": "registrá",
    "poneme": "poné",
    "decime": "decime",
    "contame": "contame",
    "mandame": "mandá",
    "subime": "subí",
    "bajame": "bajá",
    "mostrame": "mostrá",
    "dame": "dame",
    # Frases armadas
    "entró plata": "ingresó dinero",
    "salió plata": "salió dinero",
    "entró guita": "ingresó dinero",
    "salió guita": "salió dinero",
    "me pagaron": "cobré",
    "me deben": "cuentas por cobrar",
    "le debo": "deuda a proveedor",
    "le debo al": "deuda a proveedor",
    "al toque": "inmediatamente",
    "re caro": "muy caro",
    "re barato": "muy barato",
    "re bien": "muy bien",
    "más o menos": "aproximadamente",
    "más o meno": "aproximadamente",
    "un toco": "mucho",
    "un camión": "mucho",
    "un montón": "mucho",
    # Productos genéricos de kiosco/almacén
    "birra": "cerveza",
    "birras": "cervezas",
    "vianda": "comida preparada",
    "combo": "producto combinado",
    "surtido": "variedad de productos",
    # Tiempo
    "hoy a la mañana": "hoy por la mañana",
    "hoy a la tarde": "hoy por la tarde",
    "ayer a la mañana": "ayer por la mañana",
    "el finde": "el fin de semana",
    # Acciones de negocio
    "remarcar": "actualizar precio",
    "remarcación": "actualización de precio",
    "remarqué": "actualicé el precio",
    "remarcaste": "actualizaste el precio",
    "hacer caja": "cerrar caja",
    "cuadrar la caja": "cerrar caja",
    "tirar precios": "actualizar precios",
    "tirar una lista": "enviar lista de precios",
    "hacer un pedido": "registrar orden de compra",
    "hacer el pedido": "registrar orden de compra",
    "pedir al proveedor": "registrar orden de compra",
    "pedir mercadería": "registrar orden de compra",
    "llegó el pedido": "recibí mercadería",
    "llegó la merca": "recibí mercadería",
    "se vino el proveedor": "llegó el proveedor",
    "quedé sin stock": "sin stock",
    "se me acabó": "sin stock",
    "me queda poco": "stock bajo",
    "me estoy quedando sin": "stock bajo de",
    "se vendió todo": "sin stock",
}

# Frases múltiples ordenadas por longitud (primero las más largas)
_SLANG_PHRASES = sorted(
    [(k, v) for k, v in _SLANG.items() if " " in k],
    key=lambda x: len(x[0]),
    reverse=True,
)
_SLANG_WORDS = {k: v for k, v in _SLANG.items() if " " not in k}

# ── Regex de entidades de negocio ─────────────────────────────────────────────
_MONEY_RE = re.compile(
    r"\$\s*[\d.,]+"
    r"|\b\d{1,3}(?:[.\s]\d{3})+"
    r"|\b\d+(?:,\d+)+"
    r"|\b\d+\s*(?:pesos?|ars|mangos?)\b",
    re.IGNORECASE,
)
_QTY_RE = re.compile(
    r"\b(\d+)\s+(?:unidades?|u\b|cajas?|packs?|doc(?:enas?)?|kg|kilos?|litros?|l\b)\b",
    re.IGNORECASE,
)
_PCT_RE = re.compile(r"\b(\d{1,3}(?:[.,]\d+)?)\s*(?:%|por\s*ciento)\b", re.IGNORECASE)


@dataclass
class PreprocessResult:
    original_message: str
    normalized_message: str
    entities: dict[str, Any] = field(default_factory=dict)
    # True si spacy estaba disponible
    spacy_used: bool = False


def _normalize_slang(text: str) -> str:
    """Reemplaza frases y palabras de lunfardo por equivalentes en español estándar."""
    lower = text.lower()
    # Primero frases (más largas primero para evitar reemplazos parciales)
    for slang, std in _SLANG_PHRASES:
        if slang in lower:
            lower = lower.replace(slang, std)
    # Luego palabras individuales
    tokens = lower.split()
    normalized = []
    for tok in tokens:
        clean = tok.strip(".,;:!¿?¡()")
        if clean in _SLANG_WORDS:
            normalized.append(tok.replace(clean, _SLANG_WORDS[clean]))
        else:
            normalized.append(tok)
    return " ".join(normalized)


def _extract_entities_simple(message: str) -> dict[str, Any]:
    """Extracción de entidades con regex puro (sin spacy)."""
    entities: dict[str, Any] = {}

    # Montos
    money_matches = _MONEY_RE.findall(message)
    if money_matches:
        entities["amount_hints"] = money_matches

    # Cantidades con unidad
    qty_matches = _QTY_RE.findall(message)
    if qty_matches:
        entities["quantity_hints"] = [int(q) for q in qty_matches]

    # Porcentajes
    pct_matches = _PCT_RE.findall(message)
    if pct_matches:
        entities["percentage_hints"] = [float(p.replace(",", ".")) for p in pct_matches]

    return entities


def preprocess(message: str) -> PreprocessResult:
    """Normaliza el mensaje y extrae entidades pre-identificadas.

    Intenta usar spacy si está disponible; si no, cae a regex puro.
    La normalización de lunfardo siempre se aplica.
    """
    if not message or not message.strip():
        return PreprocessResult(original_message=message, normalized_message=message)

    # Paso 1: normalizar lunfardo/rioplatense
    normalized = _normalize_slang(message)

    # Paso 2: extraer entidades
    entities = _extract_entities_simple(normalized)
    spacy_used = False

    try:
        # Intentar cargar modelo español; si spacy no está instalado o el modelo no existe
        # _get_nlp() lanzará ImportError u OSError → usamos solo regex
        nlp = _get_nlp()
        doc = nlp(normalized)
        spacy_used = True

        # Entidades detectadas por spacy (ORG=proveedor, MONEY=monto, MISC=producto)
        for ent in doc.ents:
            label = ent.label_.lower()
            if label in ("money", "quantity"):
                entities.setdefault("spacy_amounts", []).append(ent.text)
            elif label == "org":
                entities.setdefault("spacy_organizations", []).append(ent.text)
            elif label == "per":
                entities.setdefault("spacy_persons", []).append(ent.text)
            elif label == "loc":
                entities.setdefault("spacy_locations", []).append(ent.text)

        # Verbos lematizados (acción del usuario)
        action_lemmas = [
            token.lemma_
            for token in doc
            if token.pos_ == "VERB" and not token.is_stop and len(token.lemma_) > 3
        ]
        if action_lemmas:
            entities["action_lemmas"] = action_lemmas[:3]

    except (ImportError, OSError):
        # spacy no instalado o modelo no disponible — funciona con regex puro
        pass

    return PreprocessResult(
        original_message=message,
        normalized_message=normalized,
        entities=entities,
        spacy_used=spacy_used,
    )


_nlp_cache: Any = None


def _get_nlp() -> Any:
    """Carga el modelo spacy una vez y lo cachea (singleton)."""
    global _nlp_cache  # noqa: PLW0603
    if _nlp_cache is None:
        import spacy  # noqa: PLC0415

        _nlp_cache = spacy.load("es_core_news_sm")
    return _nlp_cache
