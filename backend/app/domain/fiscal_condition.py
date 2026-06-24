"""Condición fiscal del negocio — catálogo canónico + normalizador tolerante.

Single source of truth para los valores de ``business_profiles.fiscal_condition``.
La columna es ``Text`` nullable (sin enum de Postgres, para mantener el esquema
additive/reversible); la validación vive 100% en la capa de aplicación.

Valores canónicos (3) + ``None`` = no configurado:
    - ``monotributo``           → contribuyente monotributista
    - ``responsable_inscripto`` → IVA responsable inscripto
    - ``informal``              → sin inscripción / venta informal

El régimen fiscal es **SOLO INFORMATIVO**: mejora heurísticas de impuestos y
márgenes y adapta la guía del arqueo en la UI. NUNCA bloquea el uso de la
plataforma ni cambia ningún cálculo determinístico.

Compatibilidad legacy: el valor viejo ``registered`` (monotributo/RI juntos)
debe seguir leyéndose sin romper. ``normalize_fiscal_condition`` lo mapea a
``monotributo`` por defecto (caso más común en negocios argentinos). Para lógica
que solo necesite saber "está registrado o no", usar ``is_registered``.
"""

from __future__ import annotations

from typing import Final, Literal

from app.observability.logger import get_logger

logger = get_logger(__name__)

# ── Valores canónicos ────────────────────────────────────────────────────────

# Tipo estrecho para schemas Pydantic y firmas (None = no configurado).
FiscalCondition = Literal["monotributo", "responsable_inscripto", "informal"]

MONOTRIBUTO: Final[FiscalCondition] = "monotributo"
RESPONSABLE_INSCRIPTO: Final[FiscalCondition] = "responsable_inscripto"
INFORMAL: Final[FiscalCondition] = "informal"

#: Conjunto cerrado de valores aceptados por la capa de aplicación.
CANONICAL_FISCAL_CONDITIONS: frozenset[str] = frozenset(
    {MONOTRIBUTO, RESPONSABLE_INSCRIPTO, INFORMAL}
)

#: Valores que representan un negocio fiscalmente registrado (no informal).
_REGISTERED: frozenset[str] = frozenset({MONOTRIBUTO, RESPONSABLE_INSCRIPTO})

# Aliases tolerantes → código canónico. Claves normalizadas (lower, sin espacios
# extra). Incluye el legacy ``registered`` y variantes habituales del dominio.
_ALIASES: dict[str, FiscalCondition] = {
    # Legacy: 'registered' agrupaba monotributo + RI. Default → monotributo.
    "registered": MONOTRIBUTO,
    # Monotributo
    "monotributo": MONOTRIBUTO,
    "monotributista": MONOTRIBUTO,
    "mono": MONOTRIBUTO,
    "monotributo social": MONOTRIBUTO,
    # Responsable inscripto
    "responsable_inscripto": RESPONSABLE_INSCRIPTO,
    "responsable inscripto": RESPONSABLE_INSCRIPTO,
    "responsable inscripta": RESPONSABLE_INSCRIPTO,
    "responsable_inscripta": RESPONSABLE_INSCRIPTO,
    "ri": RESPONSABLE_INSCRIPTO,
    "iva responsable inscripto": RESPONSABLE_INSCRIPTO,
    # Informal
    "informal": INFORMAL,
    "no inscripto": INFORMAL,
    "no_inscripto": INFORMAL,
    "sin inscripcion": INFORMAL,
    "ninguno": INFORMAL,
    "none": INFORMAL,
}


def _norm_key(value: str) -> str:
    """lower + colapsa espacios/guiones. No quita tildes (los valores son ASCII)."""
    return " ".join(value.strip().lower().replace("-", " ").split())


def normalize_fiscal_condition(value: str | None) -> FiscalCondition | None:
    """Normaliza texto libre/legacy a un valor canónico, o ``None``.

    - ``None`` / cadena vacía → ``None`` (no configurado).
    - Valor reconocido (incluido el legacy ``registered``) → código canónico.
    - Valor desconocido → ``None`` (no inventa; fail-soft, ya que es informativo).
    """
    if value is None:
        return None
    key = _norm_key(value)
    if not key:
        return None
    canonical = _ALIASES.get(key)
    if canonical is None:
        # No vaciar en silencio: un valor no vacío que no reconocemos es señal de
        # un alias faltante o de datos corruptos. Se descarta a None (contrato
        # intacto) pero queda registrado para no perder la pista.
        logger.warning(
            "fiscal_condition_unrecognized",
            normalized_key=key,
        )
    return canonical


def is_registered(value: str | None) -> bool:
    """``True`` si el negocio está fiscalmente registrado (monotributo o RI).

    Tolerante al legacy ``registered`` y a cualquier alias canonicalizable.
    ``informal``, ``None`` y valores desconocidos → ``False``.
    """
    canonical = normalize_fiscal_condition(value)
    return canonical in _REGISTERED
