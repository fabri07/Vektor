"""Catálogo canónico de categorías de gasto + normalizador.

Single source of truth: la API (regex de validación), el import, los agentes y
el frontend (lista espejada en `frontend/src/lib/expenseCategories.ts`) derivan
de este módulo. Cualquier texto libre (CSV importado, output de LLM, payload
viejo) se normaliza acá a un código canónico; si no hay match, cae a ``OTHER``
preservando el texto original como label (en ``custom_fields["category_label"]``).
"""

from __future__ import annotations

import re
import unicodedata

from rapidfuzz import fuzz, process

from app.domain.verticals import Vertical

# ── Normalizador genérico (reusado por product_categories) ───────────────────

_FUZZY_CUTOFF = 85


def strip_accents(value: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", value) if not unicodedata.combining(c)
    )


def _norm_key(value: str) -> str:
    """lower, sin tildes, separadores colapsados a un espacio (token-friendly)."""
    s = strip_accents(value.strip().lower())
    return re.sub(r"[\s\-_/]+", " ", s)


def _fold_plural(key: str) -> str:
    """Pliega el plural de cada palabra: ``alfombras`` y ``alfombra`` colapsan.

    Existe porque la tabla de alias está escrita en plural ("alfombras",
    "cortinas", "lamparas") y los nombres de producto reales vienen en singular
    ("alfombra shaggy", "cortina black out"). Medido sobre los nombres reales de
    una cuenta: sin plegar, la inferencia no reconocía casi nada — y lo poco que
    reconocía lo hacía por la palabra equivocada ("alfombra felpuda exterior"
    daba JARDIN porque matcheaba "exterior" y no "alfombra").

    Sólo se pliega desde 4 caracteres: sin ese piso "mes" se convierte en "me" y
    "gas" en "ga", que son alias reales de gastos.

    **No se usa en ``normalize``, a propósito.** Ahí el usuario DECLARÓ una
    categoría y la comparación exacta es parte del contrato; acá se está
    adivinando desde texto libre, que es donde la tolerancia hace falta.
    """
    return " ".join(
        re.sub(r"(?:es|s)$", "", w) if len(w) >= 4 else w for w in key.split(" ")
    )


class CategoryNormalizer:
    """Normaliza texto libre a un catálogo cerrado, en orden determinístico:

    1. Alias exacto (lower, sin tildes).
    2. Alias como palabra(s) completa(s) dentro del texto, probando el alias más
       largo primero ("Seguro comercio" → ``seguro`` → INSURANCE; ante varios
       candidatos gana el más específico, nunca un empate azaroso).
    3. Fuzzy ``ratio`` ≥ 85 contra los alias — red para typos ("mantenimento").

    El fallback NUNCA inventa códigos: devuelve ``fallback_code`` y el texto
    original como label para no perder información (reversible).
    """

    def __init__(
        self,
        codes: tuple[str, ...],
        aliases: dict[str, str],
        fallback_code: str,
    ) -> None:
        self.codes = codes
        self.fallback_code = fallback_code
        self._aliases = {_norm_key(k): v for k, v in aliases.items()}
        # Los códigos canónicos son alias de sí mismos (passthrough "RENT" → RENT).
        for code in codes:
            self._aliases.setdefault(_norm_key(code), code)
        # Para el paso 2: alias más largos (más específicos) primero; desempate
        # alfabético para que el resultado sea estable.
        self._keys_by_len = sorted(self._aliases, key=lambda k: (-len(k), k))

    def normalize(self, raw: str | None) -> tuple[str, str | None]:
        """Devuelve ``(code, label)``. ``label`` solo viene cuando cae al fallback."""
        if raw is None:
            return self.fallback_code, None
        cleaned = str(raw).strip()
        if not cleaned:
            return self.fallback_code, None

        key = _norm_key(cleaned)
        hit = self._aliases.get(key)
        if hit is not None:
            return hit, None

        for alias_key in self._keys_by_len:
            if re.search(rf"(?:^| ){re.escape(alias_key)}(?: |$)", key):
                return self._aliases[alias_key], None

        match = process.extractOne(
            key,
            self._aliases.keys(),
            scorer=fuzz.ratio,
            score_cutoff=_FUZZY_CUTOFF,
        )
        if match is not None:
            return self._aliases[match[0]], None

        return self.fallback_code, cleaned[:50]

    def codes_present(self, raw: str | None) -> set[str]:
        """TODOS los códigos cuyo alias aparece como palabra completa en ``raw``.

        Es el paso 2 de :meth:`normalize` sin su desempate: ahí, ante dos alias
        posibles, gana el más largo — correcto cuando el usuario DECLARÓ una
        categoría y hay que elegir una. Acá el caso es el opuesto: inferir desde
        un texto que nadie declaró (el nombre de un producto), donde dos
        categorías posibles significan que no se sabe, no que gane la más larga.
        Devolver el conjunto deja esa decisión afuera, en quien infiere.

        Sin fuzzy a propósito: un typo dentro de un nombre de producto no es
        evidencia de nada, y el paso 3 de ``normalize`` compara el texto ENTERO
        contra cada alias — sobre "almohadones lienzo 30 x 50" mediría cualquier
        cosa. Tampoco cae al fallback: la ausencia de evidencia se devuelve como
        conjunto vacío, no como ``OTHER``.
        """
        if raw is None:
            return set()
        key = _fold_plural(_norm_key(str(raw).strip()))
        if not key:
            return set()
        return {
            code
            for alias_key, code in self._aliases.items()
            if re.search(rf"(?:^| ){re.escape(_fold_plural(alias_key))}(?: |$)", key)
        }


# ── Catálogo de gastos ────────────────────────────────────────────────────────

EXPENSE_CATEGORY_CODES: tuple[str, ...] = (
    "RENT",
    "UTILITIES",
    "PAYROLL",
    "INVENTORY",
    "MARKETING",
    "BANK_FEES",
    "TAXES",
    "INSURANCE",
    "LOGISTICS",
    "MAINTENANCE",
    "SUPPLIES",
    "PROFESSIONAL_SERVICES",
    "OTHER",
)

EXPENSE_CATEGORY_LABELS_ES: dict[str, str] = {
    "RENT": "Alquiler",
    "UTILITIES": "Servicios",
    "PAYROLL": "Sueldos",
    "INVENTORY": "Mercadería",
    "MARKETING": "Marketing",
    "BANK_FEES": "Comisiones bancarias",
    "TAXES": "Impuestos",
    "INSURANCE": "Seguros",
    "LOGISTICS": "Logística",
    "MAINTENANCE": "Mantenimiento",
    "SUPPLIES": "Insumos",
    "PROFESSIONAL_SERVICES": "Profesionales",
    "OTHER": "Otros",
}

EXPENSE_CATEGORIES_PATTERN = r"^(" + "|".join(EXPENSE_CATEGORY_CODES) + r")$"

# Aliases es-AR (claves se normalizan: lower, sin tildes). Absorbe el viejo
# _EXPENSE_CATEGORY_MAP de cash_service más el vocabulario real de archivos.
_EXPENSE_ALIASES: dict[str, str] = {
    # RENT
    "alquiler": "RENT",
    "alquiler local": "RENT",
    "renta": "RENT",
    # UTILITIES
    "servicios": "UTILITIES",
    "service": "UTILITIES",
    "luz": "UTILITIES",
    "electricidad": "UTILITIES",
    "gas": "UTILITIES",
    "agua": "UTILITIES",
    "internet": "UTILITIES",
    "telefono": "UTILITIES",
    # PAYROLL
    "sueldos": "PAYROLL",
    "sueldo": "PAYROLL",
    "salarios": "PAYROLL",
    "empleados": "PAYROLL",
    "cargas sociales": "PAYROLL",
    # INVENTORY
    "inventario": "INVENTORY",
    "mercaderia": "INVENTORY",
    "stock": "INVENTORY",
    "compra proveedor": "INVENTORY",  # literal legacy de REGISTER_PURCHASE
    "compras": "INVENTORY",
    "reposicion": "INVENTORY",
    # MARKETING
    "publicidad": "MARKETING",
    "carteleria": "MARKETING",
    "promocion": "MARKETING",
    # BANK_FEES
    "bancarios": "BANK_FEES",
    "bancario": "BANK_FEES",
    "comision": "BANK_FEES",
    "comisiones": "BANK_FEES",
    "comisiones bancarias": "BANK_FEES",
    "gastos bancarios": "BANK_FEES",
    "mercadopago": "BANK_FEES",
    # TAXES
    "impuestos": "TAXES",
    "impuesto": "TAXES",
    "iibb": "TAXES",
    "ingresos brutos": "TAXES",
    "monotributo": "TAXES",
    "afip": "TAXES",
    "arca": "TAXES",
    "abl": "TAXES",
    "municipal": "TAXES",
    "tasas": "TAXES",
    # INSURANCE
    "seguros": "INSURANCE",
    "seguro": "INSURANCE",
    "art": "INSURANCE",
    # LOGISTICS
    "logistica": "LOGISTICS",
    "combustible": "LOGISTICS",
    "nafta": "LOGISTICS",
    "flete": "LOGISTICS",
    "fletes": "LOGISTICS",
    "envios": "LOGISTICS",
    "envio": "LOGISTICS",
    "delivery": "LOGISTICS",
    "transporte": "LOGISTICS",
    # MAINTENANCE
    "mantenimiento": "MAINTENANCE",
    "reparacion": "MAINTENANCE",
    "reparaciones": "MAINTENANCE",
    "limpieza local": "MAINTENANCE",
    "plagas": "MAINTENANCE",
    # SUPPLIES
    "insumos": "SUPPLIES",
    "insumo": "SUPPLIES",
    "papeleria": "SUPPLIES",
    "bolsas": "SUPPLIES",
    "libreria": "SUPPLIES",
    # PROFESSIONAL_SERVICES
    "profesionales": "PROFESSIONAL_SERVICES",
    "contador": "PROFESSIONAL_SERVICES",
    "honorarios": "PROFESSIONAL_SERVICES",
    "abogado": "PROFESSIONAL_SERVICES",
    "gestoria": "PROFESSIONAL_SERVICES",
    # OTHER (legacy del import / salidas de caja)
    "otros": "OTHER",
    "otro": "OTHER",
    "importado": "OTHER",
    "salida caja": "OTHER",
    "varios": "OTHER",
}

_normalizer = CategoryNormalizer(
    codes=EXPENSE_CATEGORY_CODES,
    aliases=_EXPENSE_ALIASES,
    fallback_code="OTHER",
)


def normalize_expense_category(raw: str | None) -> tuple[str, str | None]:
    """Texto libre → ``(código canónico, label)``.

    ``label`` viene poblado solo cuando no hubo match (código OTHER) para
    preservar el texto original en ``custom_fields["category_label"]``.
    """
    return _normalizer.normalize(raw)


def classify_expense_with_vertical(
    raw: str | None,
    vertical: Vertical,
) -> tuple[str, str | None, bool]:
    """Texto libre → ``(código, label, es_mercaderia)`` considerando el vertical.

    Regla contable: una categoría de gasto que matchea el catálogo de PRODUCTOS
    del vertical ("Bebidas", "Golosinas", "Textiles"...) es una compra de
    mercadería → ``INVENTORY`` (expense_type COGS), preservando el texto
    original como label. Solo se consulta el catálogo de productos cuando el
    catálogo de gastos no reconoció el texto (fallback OTHER), así "Alquiler"
    o "Mercadería" siguen resolviendo igual que siempre.
    """
    code, label = _normalizer.normalize(raw)
    if code != "OTHER" or raw is None:
        return code, label, code == "INVENTORY"

    from app.domain.product_categories import (  # noqa: PLC0415 — evita import circular
        normalize_product_category,
    )

    prod_code, prod_label = normalize_product_category(raw, vertical)
    if prod_code != "OTHER" and prod_label is None:
        # Match real contra el catálogo del vertical → mercadería.
        return "INVENTORY", str(raw).strip()[:50], True
    return code, label, False


def infer_expense_type(
    category: str,
    product_id: object | None = None,
    explicit: str | None = None,
) -> str:
    """Discriminador contable único: OPEX (operativo) vs COGS (mercadería).

    Regla canónica para TODOS los caminos de escritura (chat, import,
    REGISTER_PURCHASE, reclasificación): un valor explícito válido gana;
    si no, es COGS cuando hay producto del catálogo vinculado o la
    categoría es INVENTORY.
    """
    if explicit in ("OPEX", "COGS"):
        return explicit
    return "COGS" if (product_id is not None or category == "INVENTORY") else "OPEX"
