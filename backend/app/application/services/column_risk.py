"""F8a — contrato de riesgo contextual de columnas con alto porcentaje de nulos.

Un porcentaje alto de nulos es un DIAGNÓSTICO contextual, nunca una decisión
automática. Este módulo deriva el riesgo DESPUÉS de conocer el mapeo efectivo
(columna → campo canónico) por contexto, y solo hace accionables:

- los targets requeridos por ``REQUIRED_FIELDS``; y
- los targets opcionales que el usuario seleccionó explícitamente
  (``user_selected=True``).

Un opcional automapeado (heurística/fuzzy/LLM/historial) que el usuario no tocó
se puede surface como advertencia (``high_null_ratio``) pero NUNCA es accionable:
aceptar pasivamente una sugerencia no convierte un opcional en obligatorio.

El helper es PURO (sin DB, sin LLM). ``affected_rows`` es EXACTO respecto del
CONFIRM: se calcula recorriendo las filas reales del contexto y contando las que
están vacías (``null_rows``) o cuyo valor NO vacío sería RECHAZADO/RUTEADO por el
importador real (``invalid_rows``), nunca ``round(null_ratio * row_count)``.

Exactitud de ``invalid_rows`` — solo cuentan los campos donde el importador
RECHAZA/RUTEA un valor no vacío. Los campos que el importador COERCE (nunca
rechaza) tienen ``invalid_rows == 0`` (solo su vaciedad importa):

- ``amount`` → ``_parse_amount`` (descarta ≤0 y no numérico → la fila no se
  inserta / se rutea) — se REUSA el parser real, no ``normalize_numeric``.
- ``transaction_date`` / ``expense_date`` → ``parse_business_datetime``; ilegible
  ⇒ la fila va a Otros (F6, invariante 2d).
- ``dni``/``cuit``/``cuil`` (maestros) → validadores fiscales; inválido ⇒ el
  maestro no se persiste (F7).
- COERCIDOS, sin detección de inválido: ``quantity``/``stock_units``
  (``_parse_qty`` coerce a 0), ``sale_price_ars``/``unit_cost_ars`` (coerce),
  ``birthday`` (``_coerce_birthday`` coerce a None), ``expiry_date``/
  ``acquired_at`` (coerce a None; el producto igual se crea).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.application.services.column_mapping_service import REQUIRED_FIELDS
from app.application.services.file_parsing import (
    _NULL_STRINGS,
    _TYPE_TO_ENTITY,
    NULL_COLUMN_WARN_THRESHOLD,
)
from app.application.services.ingestion_import_service import _parse_amount
from app.domain.date_parsing import parse_business_datetime
from app.schemas._ar_fiscal import validate_cuit, validate_dni
from app.schemas.ingestion import ColumnRiskDecision

# Buckets del summary que contienen las filas COMPLETAS del archivo (no solo el
# preview de 10). En multi-hoja/texto las filas llevan un marcador ``__context__``;
# en single-sheet no, y su contexto sintético es siempre ``"table"``.
_ROW_BUCKETS: tuple[str, ...] = (
    "ventas_detectadas",
    "gastos_detectados",
    "stock_detectado",
    "clientes_detectados",
    "proveedores_detectados",
    "otros_detectados",
)

_ACTIONABLE_REQUIREMENTS = frozenset({"required", "explicitly_selected"})

# entity_type del contexto → clave de bucket en confirmed_fields (inclusión legacy).
_ENTITY_TO_CONFIRM_KEY: dict[str, str] = {
    "sale": "ventas",
    "expense": "gastos",
    "product": "productos",
    "customer": "clientes",
    "supplier": "proveedores",
}


@dataclass(frozen=True)
class MappingEntry:
    """Una columna del archivo mapeada a un campo canónico, en un contexto.

    ``mapping_source`` = de dónde vino la SUGERENCIA (tenant_history/heuristic/
    fuzzy/llm/none). ``user_selected`` = el usuario cambió/confirmó/creó este
    mapping (lo marca el frontend; nunca se infiere de la mera presencia).
    """

    source_column: str
    target_field: str
    mapping_source: str = "none"
    user_selected: bool = False


def _is_null(value: object) -> bool:
    """Espeja el criterio de nulo de ``compute_column_null_stats``."""
    if value is None:
        return True
    return str(value).strip().lower() in _NULL_STRINGS


def _valid_date(value: object) -> bool:
    return parse_business_datetime(value) is not None


def _valid_amount(value: object) -> bool:
    # Reusa el parser REAL del importador: descarta ≤0 y no numérico.
    return _parse_amount(value) is not None


def _valid_fiscal(validator: Callable[[str | None], str | None]) -> Callable[[object], bool]:
    def _check(value: object) -> bool:
        try:
            validator(str(value))
        except Exception:  # noqa: BLE001 — cualquier error de validación = inválido
            return False
        return True

    return _check


# target canónico → validador de valor NO vacío (True = el importador lo acepta).
# Solo los campos que el importador RECHAZA/RUTEA. Los coercidos NO están acá
# (invalid_rows==0 para ellos) — ver docstring del módulo.
_TARGET_VALIDATORS: dict[str, Callable[[object], bool]] = {
    "amount": _valid_amount,
    "transaction_date": _valid_date,
    "expense_date": _valid_date,
    "dni": _valid_fiscal(validate_dni),
    "customer_dni": _valid_fiscal(validate_dni),
    "cuit": _valid_fiscal(validate_cuit),
    "customer_cuit": _valid_fiscal(validate_cuit),
    "cuil": _valid_fiscal(validate_cuit),
    "supplier_cuil": _valid_fiscal(validate_cuit),
}


def _is_real_target(target_field: str | None) -> bool:
    """Un target participa del protocolo solo si es un campo canónico real.

    ``ignore`` y los custom fields quedan fuera (siempre opcionales, sin validador
    canónico; su tratamiento de nulos no es parte de F8a)."""
    if not target_field or target_field == "ignore":
        return False
    return not target_field.startswith("custom_field:")


def resolve_contexts(summary: dict[str, Any]) -> list[dict[str, Any]]:
    """Contextos del summary, o un contexto sintético ``table`` para summaries
    legacy sin ``mapping_contexts`` (fallback: usa ``inferred_type`` + ``headers`` +
    los buckets planos). Si no hay ``headers`` o el tipo no mapea a entidad, no hay
    diagnóstico F8 (lista vacía)."""
    ctxs = summary.get("mapping_contexts")
    if ctxs:
        return [c for c in ctxs if isinstance(c, dict)]

    headers = summary.get("headers")
    inferred = summary.get("inferred_type")
    if not headers or not inferred:
        return []
    entity = _TYPE_TO_ENTITY.get(inferred)
    if not entity:
        return []
    return [
        {
            "context_id": "table",
            "entity_type": entity,
            "headers": list(headers),
            "preview_rows": summary.get("preview_rows") or [],
            "row_count": summary.get("row_count", 0),
        }
    ]


def context_is_included(
    context_id: str,
    entity_type: str | None,
    confirmed_fields: dict[str, bool],
    context_confirmed: dict[str, bool],
) -> bool:
    """Misma decisión de inclusión que el confirm (``_context_included``): por
    contexto si vino ``context_confirmed``; si no, gating por tipo vía
    ``confirmed_fields``. Fuente única para evitar drift entre confirm y F8."""
    if context_confirmed:
        return bool(context_confirmed.get(context_id, False))
    key = _ENTITY_TO_CONFIRM_KEY.get(entity_type or "")
    return bool(key and confirmed_fields.get(key))


def _rows_for_context(summary: dict[str, Any], context_id: str) -> list[dict[str, Any]]:
    """Filas COMPLETAS de un contexto, desde los buckets del summary.

    Single-sheet: las filas no llevan ``__context__`` y su contexto es ``"table"``
    (``r.get("__context__", "table")``). Multi-hoja/texto: filtradas por marcador.
    """
    rows: list[dict[str, Any]] = []
    for bucket in _ROW_BUCKETS:
        for row in summary.get(bucket) or []:
            if not isinstance(row, dict):
                continue
            if row.get("__context__", "table") == context_id:
                rows.append(row)
    return rows


def build_contextual_column_risk(
    summary: dict[str, Any],
    context_mappings: dict[str, list[MappingEntry]],
    *,
    context_entities: dict[str, str] | None = None,
    confirmed_fields: dict[str, bool] | None = None,
    context_confirmed: dict[str, bool] | None = None,
    null_threshold: float = NULL_COLUMN_WARN_THRESHOLD,
) -> list[dict[str, Any]]:
    """Deriva el riesgo contextual por (contexto, columna mapeada).

    ``context_entities``: entidad EFECTIVA por contexto (override de reasignación
    del usuario). Si falta, se usa la del summary. Es lo que decide
    ``REQUIRED_FIELDS`` — sin esto, reasignar general→sale usaría los requeridos
    equivocados.

    ``confirmed_fields``/``context_confirmed``: si alguno viene (no None), los
    contextos EXCLUIDOS del import no generan riesgo (misma inclusión que confirm).
    Si ambos son None (preview), se incluyen todos (informativo).

    Emite una entrada cuando:
    - target accionable (requerido / explícitamente seleccionado) con
      ``affected_rows > 0`` o ``null_ratio > threshold``; o
    - opcional no accionable con ``null_ratio > threshold`` (solo informativo,
      ``allowed_actions=[]``).

    ``required_missing`` NO se fabrica acá: sale del validador de mappings
    requeridos del confirm.
    """
    context_entities = context_entities or {}
    apply_inclusion = confirmed_fields is not None or context_confirmed is not None
    contexts = {
        c.get("context_id"): c for c in resolve_contexts(summary) if c.get("context_id")
    }
    result: list[dict[str, Any]] = []

    for context_id, entries in context_mappings.items():
        ctx = contexts.get(context_id)
        if ctx is None:
            continue
        entity = context_entities.get(context_id) or ctx.get("entity_type")
        if not entity:
            continue
        if apply_inclusion and not context_is_included(
            context_id, entity, confirmed_fields or {}, context_confirmed or {}
        ):
            continue
        required = set(REQUIRED_FIELDS.get(entity, []))

        rows = _rows_for_context(summary, context_id)
        row_count = len(rows)
        if row_count == 0:
            continue

        # target → columnas mapeadas (para saber si un requerido tiene reemplazo).
        target_to_cols: dict[str, list[str]] = defaultdict(list)
        for entry in entries:
            if _is_real_target(entry.target_field):
                target_to_cols[entry.target_field].append(entry.source_column)

        for entry in entries:
            target = entry.target_field
            if not _is_real_target(target):
                continue

            validator = _TARGET_VALIDATORS.get(target)
            null_rows = 0
            invalid_rows = 0
            for row in rows:
                value = row.get(entry.source_column)
                if _is_null(value):
                    null_rows += 1
                elif validator is not None and not validator(value):
                    invalid_rows += 1

            null_ratio = null_rows / row_count
            affected_rows = null_rows + invalid_rows
            high_null = null_ratio > null_threshold

            if target in required:
                field_requirement = "required"
            elif entry.user_selected:
                field_requirement = "explicitly_selected"
            else:
                field_requirement = "optional"
            actionable = field_requirement in _ACTIONABLE_REQUIREMENTS

            if actionable:
                if affected_rows == 0 and not high_null:
                    continue
            elif not high_null:
                continue

            allowed_actions: list[str] = []
            if actionable:
                allowed_actions.append("route_affected_rows_to_others")
                has_replacement = len(target_to_cols.get(target, [])) > 1
                if field_requirement == "explicitly_selected" or has_replacement:
                    # Un requerido con una sola columna no se puede dropear
                    # (dejaría el campo requerido sin mapear).
                    allowed_actions.append("drop_column")

            result.append(
                {
                    "context_id": context_id,
                    "entity_type": entity,
                    "source_column": entry.source_column,
                    "target_field": target,
                    "null_ratio": round(null_ratio, 4),
                    "affected_rows": affected_rows,
                    "null_rows": null_rows,
                    "invalid_rows": invalid_rows,
                    "field_requirement": field_requirement,
                    "mapping_source": entry.mapping_source,
                    "user_selected": entry.user_selected,
                    "allowed_actions": allowed_actions,
                    "recommendation": (
                        "route_affected_rows_to_others"
                        if actionable and affected_rows > 0
                        else "review"
                    ),
                }
            )
    return result


@dataclass(frozen=True)
class ColumnRiskViolation:
    """F8b: una `ColumnRiskDecision` que el mapeo efectivo NO permite.

    ``reason`` es el detalle accionable que el confirm expone en el 422 (qué
    columna, qué target, por qué)."""

    context_id: str
    source_column: str
    target_field: str
    action: str
    reason: str


def validate_column_risk_decisions(
    decisions: list[ColumnRiskDecision],
    context_mappings: dict[str, list[MappingEntry]],
    context_entities: dict[str, str],
    *,
    confirmed_fields: dict[str, bool] | None = None,
    context_confirmed: dict[str, bool] | None = None,
) -> list[ColumnRiskViolation]:
    """F8b (Task 2): valida las decisiones del usuario ANTES del lease.

    PURA (sin DB, sin LLM) — mismo espíritu que ``build_contextual_column_risk``:
    recibe el mapeo efectivo + la entidad efectiva por contexto y determina qué
    decisiones violan el contrato:

    - ``drop_column`` de un target REQUERIDO (``REQUIRED_FIELDS`` de la entidad
      efectiva) sin OTRA columna mapeada al mismo ``target_field`` en ese
      contexto: dejaría el requerido sin mapear.
    - ``route_affected_rows_to_others`` de un target NO accionable (opcional
      que el usuario no seleccionó explícitamente): invariante 1 — un opcional
      vacío nunca manda filas a Otros.

    Decisiones sobre un contexto EXCLUIDO del import (misma inclusión que el
    confirm, vía ``context_is_included``) no generan violación — esas filas ni
    se procesan. Si ambos filtros de inclusión son ``None`` (no se pasaron), no
    se aplica exclusión (se asume que el caller ya filtró).
    """
    apply_inclusion = confirmed_fields is not None or context_confirmed is not None
    violations: list[ColumnRiskViolation] = []

    for decision in decisions:
        entity = context_entities.get(decision.context_id)
        if not entity:
            continue
        if apply_inclusion and not context_is_included(
            decision.context_id, entity, confirmed_fields or {}, context_confirmed or {}
        ):
            continue

        entries = context_mappings.get(decision.context_id, [])
        target_to_cols: dict[str, list[str]] = defaultdict(list)
        matched_entry: MappingEntry | None = None
        for entry in entries:
            if _is_real_target(entry.target_field):
                target_to_cols[entry.target_field].append(entry.source_column)
            if (
                entry.source_column == decision.source_column
                and entry.target_field == decision.target_field
            ):
                matched_entry = entry

        required = set(REQUIRED_FIELDS.get(entity, []))
        if decision.target_field in required:
            field_requirement = "required"
        elif matched_entry is not None and matched_entry.user_selected:
            field_requirement = "explicitly_selected"
        else:
            field_requirement = "optional"

        if decision.action == "drop_column":
            has_replacement = len(target_to_cols.get(decision.target_field, [])) > 1
            if field_requirement == "required" and not has_replacement:
                violations.append(
                    ColumnRiskViolation(
                        context_id=decision.context_id,
                        source_column=decision.source_column,
                        target_field=decision.target_field,
                        action=decision.action,
                        reason=(
                            f"No se puede eliminar la columna '{decision.source_column}': "
                            f"es la única mapeada al campo requerido "
                            f"'{decision.target_field}' en el contexto "
                            f"'{decision.context_id}'."
                        ),
                    )
                )
        elif decision.action == "route_affected_rows_to_others":
            if field_requirement not in _ACTIONABLE_REQUIREMENTS:
                violations.append(
                    ColumnRiskViolation(
                        context_id=decision.context_id,
                        source_column=decision.source_column,
                        target_field=decision.target_field,
                        action=decision.action,
                        reason=(
                            f"'{decision.target_field}' es un campo opcional que el usuario "
                            "no seleccionó explícitamente: un opcional vacío no puede "
                            "rutear filas a Otros."
                        ),
                    )
                )

    return violations
