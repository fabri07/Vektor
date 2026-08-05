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

import copy
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.column_mapping_service import (
    REQUIRED_FIELDS,
    ColumnMappingService,
)
from app.application.services.file_parsing import (
    _NULL_STRINGS,
    _TYPE_TO_ENTITY,
    NULL_COLUMN_WARN_THRESHOLD,
)
from app.application.services.ingestion_import_service import _parse_amount
from app.domain.date_parsing import parse_business_datetime
from app.schemas._ar_fiscal import validate_cuit, validate_dni
from app.schemas.ingestion import ColumnMapping, ColumnRiskDecision

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

# Entidades riesgosas (F8a): contextos que generan diagnóstico de riesgo contextual.
_RISK_ENTITIES = ("sale", "expense", "product", "customer", "supplier")

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


def _classify_cell(value: object, validator: Callable[[object], bool] | None) -> str | None:
    """Clasifica una celda respecto del importador canónico: ``None`` = aceptada,
    ``"null"`` = vacía, ``"invalid"`` = no vacía pero RECHAZADA/RUTEADA.

    Fuente ÚNICA del criterio de "fila afectada" — la comparten el diagnóstico
    (``build_contextual_column_risk``) y la aplicación en el confirm
    (``affected_rows_for_context``) para no divergir del parser canónico
    (invariante 3: el backend recalcula, nunca confía en un conteo del cliente)."""
    if _is_null(value):
        return "null"
    if validator is not None and not validator(value):
        return "invalid"
    return None


def _is_real_target(target_field: str | None) -> bool:
    """Un target participa del protocolo solo si es un campo canónico real.

    ``ignore`` y los custom fields quedan fuera (siempre opcionales, sin validador
    canónico; su tratamiento de nulos no es parte de F8a)."""
    from app.application.services.column_mapping_service import (  # noqa: PLC0415
        parse_target,
    )

    return parse_target(target_field).kind == "canonical"


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
                kind = _classify_cell(row.get(entry.source_column), validator)
                if kind == "null":
                    null_rows += 1
                elif kind == "invalid":
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
      efectiva) que deja el target SIN columnas sobrevivientes en ese contexto:
      dejaría el requerido sin mapear. El batch se trata ATÓMICAMENTE — dos
      ``drop_column`` del MISMO request sobre las dos únicas columnas de un
      requerido se evalúan juntas (ninguna decisión individual "ve" a la otra
      columna dropeada como si siguiera disponible; ver bug reportado por
      review, F8b Task 2).
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

    # Batch atómico: columnas que el request dropea por (context_id, target_field).
    # Sin esto, dos `drop_column` sobre las dos únicas columnas de un mismo
    # requerido se validarían una contra la otra en el snapshot ESTÁTICO de
    # `context_mappings` (cada una "ve" a la otra como reemplazo todavía mapeado)
    # y ambas pasarían, dejando el requerido sin ninguna columna sobreviviente.
    dropped_by_target: dict[tuple[str, str], set[str]] = defaultdict(set)
    for d in decisions:
        if d.action == "drop_column":
            dropped_by_target[(d.context_id, d.target_field)].add(d.source_column)

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

        if matched_entry is None:
            # El par (source_column, target_field) que declara la decisión no
            # existe en el mapeo efectivo actual del contexto — puede ser un
            # payload manipulado o una decisión stale (mapeo cambió después de
            # calcularla). Si esto pasara, `field_requirement` se derivaría
            # SOLO de `decision.target_field` (declarado por el cliente) sin
            # verificar que `decision.source_column` sea realmente la columna
            # mapeada a ese target — dejando que `apply_column_risk_decisions`
            # rutee/dropee una columna arbitraria bajo la apariencia de una
            # decisión sobre un campo requerido/seleccionado. Se rechaza ANTES
            # de evaluar acciones permitidas (bug reportado por review F8b/F8c).
            violations.append(
                ColumnRiskViolation(
                    context_id=decision.context_id,
                    source_column=decision.source_column,
                    target_field=decision.target_field,
                    action=decision.action,
                    reason=(
                        f"La columna '{decision.source_column}' no está mapeada a "
                        f"'{decision.target_field}' en el contexto '{decision.context_id}' "
                        "según el mapeo efectivo actual."
                    ),
                )
            )
            continue

        required = set(REQUIRED_FIELDS.get(entity, []))
        if decision.target_field in required:
            field_requirement = "required"
        elif matched_entry is not None and matched_entry.user_selected:
            field_requirement = "explicitly_selected"
        else:
            field_requirement = "optional"

        if decision.action == "drop_column":
            mapped_cols = set(target_to_cols.get(decision.target_field, []))
            dropped_cols = dropped_by_target.get(
                (decision.context_id, decision.target_field), set()
            )
            surviving_cols = mapped_cols - dropped_cols
            if field_requirement == "required" and not surviving_cols:
                violations.append(
                    ColumnRiskViolation(
                        context_id=decision.context_id,
                        source_column=decision.source_column,
                        target_field=decision.target_field,
                        action=decision.action,
                        reason=(
                            f"No se puede eliminar la columna '{decision.source_column}': "
                            f"el campo requerido '{decision.target_field}' del contexto "
                            f"'{decision.context_id}' quedaría sin ninguna columna mapeada "
                            "(todas sus columnas se están eliminando en este mismo pedido)."
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


# ── F8b (Task 4): aplicación de las decisiones sobre una COPIA del summary ────


def affected_rows_for_context(
    rows: list[dict[str, Any]],
    col_targets: list[tuple[str, str]],
) -> dict[int, dict[str, Any]]:
    """Filas AFECTADAS de un contexto por una o más columnas ruteadas.

    ``rows`` es la lista ORDENADA de filas del contexto (mismo orden que
    ``_rows_for_context`` / ``_iter_context_rows``). ``col_targets`` = lista de
    ``(columna, target)`` de las columnas con decisión
    ``route_affected_rows_to_others``.

    Devuelve ``{indice_de_fila_en_contexto: {columna_mala: valor_crudo}}`` — una
    entrada por fila con AL MENOS una columna vacía/inválida, combinando todas
    sus columnas malas en un solo dict (invariante 6: máx una captura por fila).

    Recalculado con el MISMO criterio que ``build_contextual_column_risk``
    (``_classify_cell`` + ``_TARGET_VALIDATORS``): el backend NUNCA confía en un
    ``affected_rows`` provisto por el cliente — lo recomputa (invariante 3)."""
    affected: dict[int, dict[str, Any]] = {}
    for idx, row in enumerate(rows):
        bad: dict[str, Any] = {}
        for source_column, target in col_targets:
            validator = _TARGET_VALIDATORS.get(target)
            if _classify_cell(row.get(source_column), validator) is not None:
                bad[source_column] = row.get(source_column)
        if bad:
            affected[idx] = bad
    return affected


def _iter_context_rows(
    summary: dict[str, Any], context_id: str
) -> list[tuple[str, int, dict[str, Any]]]:
    """``(bucket, posición_en_bucket, fila)`` de un contexto, en el orden de
    ``_ROW_BUCKETS`` (mismo que ``_rows_for_context``). La posición permite
    borrar la fila de la lista real del summary copiado. Single-sheet: las filas
    sin ``__context__`` pertenecen al contexto sintético ``"table"``."""
    out: list[tuple[str, int, dict[str, Any]]] = []
    for bucket in _ROW_BUCKETS:
        blist = summary.get(bucket)
        if not isinstance(blist, list):
            continue
        for pos, row in enumerate(blist):
            if isinstance(row, dict) and row.get("__context__", "table") == context_id:
                out.append((bucket, pos, row))
    return out


@dataclass(frozen=True)
class AppliedColumnRisk:
    """Resultado de aplicar las decisiones de riesgo sobre una COPIA del summary.

    - ``summary``: copia PROFUNDA ya mutada (el original nunca se toca).
    - ``dropped_columns``: ``{context_id: [columnas eliminadas]}``.
    - ``routed_rows``: ``{context_id: {row_index: {columna_mala: valor}}}`` — lo
      que el caller (Task 4) captura en "Otros" DENTRO del savepoint.
    - ``routed_entity``: entidad efectiva por contexto ruteado (para
      ``suggested_entity`` de la captura).
    - ``routed_totals``: total de filas del contexto ruteado (para el contador
      ``filas_riesgo_importadas`` = total − afectadas)."""

    summary: dict[str, Any]
    dropped_columns: dict[str, list[str]]
    routed_rows: dict[str, dict[int, dict[str, Any]]]
    routed_entity: dict[str, str]
    routed_totals: dict[str, int]


def apply_column_risk_decisions(
    summary: dict[str, Any],
    decisions: list[ColumnRiskDecision],
    context_entities: dict[str, str],
) -> AppliedColumnRisk:
    """Aplica las decisiones (ya validadas en Task 2) sobre una COPIA PROFUNDA del
    summary — el original (ORM-tracked) NUNCA se muta (invariante 4).

    - ``drop_column``: saca la columna de las filas de ESE contexto + de sus
      ``headers``/``preview_rows`` (por contexto y, en single-sheet, top-level) +
      de ``columns_at_risk``. Solo ese contexto, nunca otro.
    - ``route_affected_rows_to_others``: RECALCULA las filas afectadas (vacías/
      inválidas por el parser canónico, invariante 3), las SACA del bucket (no se
      importan) y las devuelve para capturarlas en "Otros". Las filas NO
      afectadas quedan y se importan normal.

    PURA (sin DB, sin LLM): la captura en "Otros" y la auditoría las hace el
    caller DENTRO del savepoint del confirm."""
    new_summary = copy.deepcopy(summary)

    drops: dict[str, set[str]] = defaultdict(set)
    routes: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for d in decisions:
        if d.action == "drop_column":
            drops[d.context_id].add(d.source_column)
        elif d.action == "route_affected_rows_to_others":
            routes[d.context_id].append((d.source_column, d.target_field))

    contexts = {
        c.get("context_id"): c
        for c in resolve_contexts(new_summary)
        if c.get("context_id")
    }

    routed_rows: dict[str, dict[int, dict[str, Any]]] = {}
    routed_entity: dict[str, str] = {}
    routed_totals: dict[str, int] = {}

    # ── ROUTE primero: captura los valores ANTES de un posible drop de otra columna
    for cid, col_targets in routes.items():
        ordered = _iter_context_rows(new_summary, cid)
        rows = [row for (_b, _p, row) in ordered]
        affected = affected_rows_for_context(rows, col_targets)
        routed_rows[cid] = affected
        routed_totals[cid] = len(ordered)
        routed_entity[cid] = (
            context_entities.get(cid)
            or str((contexts.get(cid) or {}).get("entity_type") or "")
        )
        # Borrar las filas afectadas de sus buckets (posiciones desc por bucket
        # para no correr los índices al eliminar).
        to_remove: dict[str, list[int]] = defaultdict(list)
        for idx in affected:
            bucket, pos, _row = ordered[idx]
            to_remove[bucket].append(pos)
        for bucket, positions in to_remove.items():
            blist = new_summary.get(bucket)
            if isinstance(blist, list):
                for pos in sorted(positions, reverse=True):
                    del blist[pos]

    # ── DROP después ──
    dropped_columns: dict[str, list[str]] = {}
    all_dropped_names: set[str] = set()
    for cid, cols in drops.items():
        dropped_columns[cid] = sorted(cols)
        all_dropped_names |= cols
        # Filas del contexto (sobre lo que quedó tras el route).
        for _b, _p, row in _iter_context_rows(new_summary, cid):
            for c in cols:
                row.pop(c, None)
        # headers/preview por contexto (multi-hoja).
        for ctx in new_summary.get("mapping_contexts") or []:
            if isinstance(ctx, dict) and ctx.get("context_id") == cid:
                if isinstance(ctx.get("headers"), list):
                    ctx["headers"] = [h for h in ctx["headers"] if h not in cols]
                for pr in ctx.get("preview_rows") or []:
                    if isinstance(pr, dict):
                        for c in cols:
                            pr.pop(c, None)
        # Single-sheet: headers/preview top-level (contexto sintético "table").
        if cid == "table":
            if isinstance(new_summary.get("headers"), list):
                new_summary["headers"] = [
                    h for h in new_summary["headers"] if h not in cols
                ]
            for pr in new_summary.get("preview_rows") or []:
                if isinstance(pr, dict):
                    for c in cols:
                        pr.pop(c, None)

    # columns_at_risk (diagnóstico global legacy): sacar las columnas dropeadas
    # para que la copia compacta persistida no siga marcándolas como riesgosas.
    if all_dropped_names and isinstance(new_summary.get("columns_at_risk"), list):
        new_summary["columns_at_risk"] = [
            e
            for e in new_summary["columns_at_risk"]
            if not (isinstance(e, dict) and e.get("column") in all_dropped_names)
        ]

    return AppliedColumnRisk(
        summary=new_summary,
        dropped_columns=dropped_columns,
        routed_rows=routed_rows,
        routed_entity=routed_entity,
        routed_totals=routed_totals,
    )


# ── F8a: Construcción del mapeo contextual (mudado desde ingestion.py) ────


async def derive_context_mapping_entries(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    summary: dict[str, Any],
    *,
    user_mappings: list[ColumnMapping] | None = None,
    context_entity: dict[str, str] | None = None,
) -> tuple[dict[str, list[MappingEntry]], dict[str, str]]:
    """F8a: arma el mapeo efectivo por contexto (las 5 entidades) para el motor de
    riesgo, y devuelve además la ENTIDAD EFECTIVA por contexto (override de
    reasignación aplicado) para que el helper use los ``REQUIRED_FIELDS`` correctos.

    Base = sugerencias determinísticas (``allow_llm=False`` — este builder corre en
    el preview de cada poll, igual criterio que el preview de maestros). Si
    ``user_mappings`` viene (endpoint ``/column-risk``), overlaya el target elegido
    por el usuario con ``user_selected`` real, preservando el ``mapping_source`` de
    la sugerencia de esa columna. Solo contextos con ``headers`` (tabulares); los de
    texto/OCR no tienen columnas que dropear.
    """
    context_entity = context_entity or {}
    mapping_svc = ColumnMappingService(session)

    user_by_ctx: dict[str, dict[str, ColumnMapping]] = defaultdict(dict)
    for m in user_mappings or []:
        user_by_ctx[m.context_id or "table"][m.source_column] = m

    result: dict[str, list[MappingEntry]] = {}
    effective_entities: dict[str, str] = {}
    for ctx in resolve_contexts(summary):
        context_id = ctx.get("context_id")
        headers = ctx.get("headers")
        if not context_id or not headers:
            continue
        entity = context_entity.get(context_id) or ctx.get("entity_type")
        if entity not in _RISK_ENTITIES:
            continue
        effective_entities[context_id] = entity

        sample_rows = ctx.get("preview_rows") or []
        suggestions = await mapping_svc.suggest_mappings(
            tenant_id, entity, list(headers), sample_rows, allow_llm=False
        )
        source_by_col = {s["source_column"]: s["source"] for s in suggestions}
        by_col: dict[str, MappingEntry] = {
            s["source_column"]: MappingEntry(
                source_column=s["source_column"],
                target_field=s["target_field"],
                mapping_source=s["source"],
                user_selected=False,
            )
            for s in suggestions
            if s["status"] == "mapped" and s["target_field"]
        }
        for col, mapping in user_by_ctx.get(context_id, {}).items():
            by_col[col] = MappingEntry(
                source_column=col,
                target_field=mapping.target_field,
                mapping_source=source_by_col.get(col, "none"),
                user_selected=mapping.user_selected,
            )
        if by_col:
            result[context_id] = list(by_col.values())
    return result, effective_entities


# ── F8c: Separación de decisiones derivables (función pura) ────


def split_derivable_decisions(
    risk_rows: list[dict[str, Any]],
) -> tuple[list[ColumnRiskDecision], list[dict[str, Any]]]:
    """Separa filas de riesgo contextual en decisiones forzadas y ambiguas.

    Recorre cada row (salida de `build_contextual_column_risk`):
    - Si `allowed_actions == []` (informativa, sin acción posible) → ignorar.
    - Si `len(allowed_actions) == 1` → decisión FORZADA: construir
      `ColumnRiskDecision` con esa acción única.
    - Si `len(allowed_actions) >= 2` → AMBIGUA: agregar row cruda a lista ambigua.

    Devuelve `(decisiones_forzadas, filas_ambiguas)`.
    """
    forced: list[ColumnRiskDecision] = []
    ambiguous: list[dict[str, Any]] = []

    for row in risk_rows:
        allowed = row.get("allowed_actions") or []

        if len(allowed) == 0:
            # Informativa, sin acción posible → ignorar
            continue
        elif len(allowed) == 1:
            # Decisión forzada
            action = allowed[0]
            decision = ColumnRiskDecision(
                context_id=row["context_id"],
                source_column=row["source_column"],
                target_field=row["target_field"],
                action=action,
            )
            forced.append(decision)
        else:
            # Ambigua (múltiples acciones posibles)
            ambiguous.append(row)

    return forced, ambiguous
