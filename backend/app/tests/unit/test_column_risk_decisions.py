"""F8b (Task 2) — tests del validador puro de `ColumnRiskDecision`.

`validate_column_risk_decisions` es PURO (sin DB, sin LLM): dado el mapeo
efectivo por contexto + la entidad efectiva por contexto, determina qué
decisiones del usuario son inválidas. Estos tests fijan las dos reglas del
brief de la Tarea 2:

- `drop_column` de un target REQUERIDO sin otra columna mapeada al mismo
  target en ese contexto → violación.
- `route_affected_rows_to_others` de un target NO accionable (opcional no
  seleccionado explícitamente por el usuario) → violación.

Y sus contraejemplos (con reemplazo / requerido / explícitamente seleccionado
→ sin violación), más el caso de contexto excluido del import.
"""

from __future__ import annotations

from app.application.services.column_risk import (
    MappingEntry,
    validate_column_risk_decisions,
)
from app.schemas.ingestion import ColumnRiskDecision


def test_drop_requerido_sin_reemplazo_es_violacion() -> None:
    """`transaction_date` es requerido en `sale`. Una sola columna mapeada a él:
    dropearla dejaría el requerido sin mapear → violación."""
    context_mappings = {
        "table": [
            MappingEntry("fecha", "transaction_date", user_selected=False),
            MappingEntry("monto", "amount", user_selected=False),
        ]
    }
    context_entities = {"table": "sale"}
    decisions = [
        ColumnRiskDecision(
            context_id="table",
            source_column="fecha",
            target_field="transaction_date",
            action="drop_column",
        )
    ]

    violations = validate_column_risk_decisions(decisions, context_mappings, context_entities)

    assert len(violations) == 1
    assert violations[0].context_id == "table"
    assert violations[0].source_column == "fecha"
    assert violations[0].target_field == "transaction_date"
    assert violations[0].action == "drop_column"


def test_drop_requerido_con_reemplazo_ok() -> None:
    """Dos columnas mapeadas al mismo target requerido: dropear una de ellas
    deja la otra cubriendo el campo → sin violación."""
    context_mappings = {
        "table": [
            MappingEntry("fecha", "transaction_date", user_selected=False),
            MappingEntry("fecha_alt", "transaction_date", user_selected=True),
            MappingEntry("monto", "amount", user_selected=False),
        ]
    }
    context_entities = {"table": "sale"}
    decisions = [
        ColumnRiskDecision(
            context_id="table",
            source_column="fecha",
            target_field="transaction_date",
            action="drop_column",
        )
    ]

    violations = validate_column_risk_decisions(decisions, context_mappings, context_entities)

    assert violations == []


def test_drop_ambas_columnas_del_mismo_requerido_en_un_batch_es_violacion() -> None:
    """Bug crítico (review de Task 2): dos columnas mapean a un requerido
    (transaction_date). Si el MISMO request dropea las DOS, cada decisión
    tomada aisladamente contra el snapshot estático "ve" a la otra como
    reemplazo todavía mapeado — y ambas pasarían, dejando el requerido sin
    NINGUNA columna. El batch debe evaluarse atómicamente: el conjunto de
    columnas sobrevivientes (mapeadas MENOS todas las dropeadas en este mismo
    request) debe seguir cubriendo el requerido."""
    context_mappings = {
        "table": [
            MappingEntry("fecha", "transaction_date", user_selected=False),
            MappingEntry("fecha_alt", "transaction_date", user_selected=True),
            MappingEntry("monto", "amount", user_selected=False),
        ]
    }
    context_entities = {"table": "sale"}
    decisions = [
        ColumnRiskDecision(
            context_id="table",
            source_column="fecha",
            target_field="transaction_date",
            action="drop_column",
        ),
        ColumnRiskDecision(
            context_id="table",
            source_column="fecha_alt",
            target_field="transaction_date",
            action="drop_column",
        ),
    ]

    violations = validate_column_risk_decisions(decisions, context_mappings, context_entities)

    # Ambas decisiones quedan sin columna sobreviviente → ambas se flaggean.
    assert len(violations) == 2
    flagged_columns = {v.source_column for v in violations}
    assert flagged_columns == {"fecha", "fecha_alt"}
    for v in violations:
        assert v.target_field == "transaction_date"
        assert v.action == "drop_column"


def test_route_opcional_no_seleccionado_es_violacion() -> None:
    """`notes` en `product` es opcional. Sin `user_selected=True`, rutear filas
    a Otros por su vaciedad violaría el invariante 1 (opcional vacío nunca
    manda filas a Otros) → violación."""
    context_mappings = {
        "table": [
            MappingEntry("nombre", "name", user_selected=False),
            MappingEntry("notas", "notes", user_selected=False),
        ]
    }
    context_entities = {"table": "product"}
    decisions = [
        ColumnRiskDecision(
            context_id="table",
            source_column="notas",
            target_field="notes",
            action="route_affected_rows_to_others",
        )
    ]

    violations = validate_column_risk_decisions(decisions, context_mappings, context_entities)

    assert len(violations) == 1
    assert violations[0].action == "route_affected_rows_to_others"
    assert violations[0].target_field == "notes"


def test_route_requerido_ok() -> None:
    """Rutear filas de un target requerido (amount) sí está permitido."""
    context_mappings = {
        "table": [
            MappingEntry("fecha", "transaction_date", user_selected=False),
            MappingEntry("monto", "amount", user_selected=False),
        ]
    }
    context_entities = {"table": "sale"}
    decisions = [
        ColumnRiskDecision(
            context_id="table",
            source_column="monto",
            target_field="amount",
            action="route_affected_rows_to_others",
        )
    ]

    violations = validate_column_risk_decisions(decisions, context_mappings, context_entities)

    assert violations == []


def test_route_explicitamente_seleccionado_ok() -> None:
    """Un opcional que el usuario SÍ seleccionó explícitamente puede rutear."""
    context_mappings = {
        "table": [
            MappingEntry("nombre", "name", user_selected=False),
            MappingEntry("notas", "notes", user_selected=True),
        ]
    }
    context_entities = {"table": "product"}
    decisions = [
        ColumnRiskDecision(
            context_id="table",
            source_column="notas",
            target_field="notes",
            action="route_affected_rows_to_others",
        )
    ]

    violations = validate_column_risk_decisions(decisions, context_mappings, context_entities)

    assert violations == []


def test_drop_opcional_explicitamente_seleccionado_ok() -> None:
    """Un opcional explícito se puede dropear (vuelve a no seleccionado, no
    rompe ningún requerido) aunque no tenga reemplazo."""
    context_mappings = {
        "table": [
            MappingEntry("nombre", "name", user_selected=False),
            MappingEntry("notas", "notes", user_selected=True),
        ]
    }
    context_entities = {"table": "product"}
    decisions = [
        ColumnRiskDecision(
            context_id="table",
            source_column="notas",
            target_field="notes",
            action="drop_column",
        )
    ]

    violations = validate_column_risk_decisions(decisions, context_mappings, context_entities)

    assert violations == []


def test_decision_en_contexto_excluido_no_genera_violacion() -> None:
    """Si el contexto fue excluido del import (confirmed_fields/context_confirmed),
    sus decisiones no importan — esas filas ni se procesan."""
    context_mappings = {
        "table": [
            MappingEntry("fecha", "transaction_date", user_selected=False),
        ]
    }
    context_entities = {"table": "sale"}
    decisions = [
        ColumnRiskDecision(
            context_id="table",
            source_column="fecha",
            target_field="transaction_date",
            action="drop_column",
        )
    ]

    violations = validate_column_risk_decisions(
        decisions,
        context_mappings,
        context_entities,
        confirmed_fields={},
        context_confirmed={"table": False},
    )

    assert violations == []


def test_decision_con_source_column_falso_sobre_target_requerido_es_violacion() -> None:
    """Reproduce el hallazgo de review: el mapeo real es `Monto -> amount`, pero
    la decisión declara `Notas -> amount` (columna que NUNCA estuvo mapeada a
    `amount`). Antes del fix, `field_requirement` se derivaba solo de
    `decision.target_field` (que SÍ es requerido) sin verificar que
    `source_column` fuera la columna real — dejando pasar la decisión y, en
    `apply_column_risk_decisions`, ruteando filas por el estado de una columna
    (`Notas`) que no tiene nada que ver con el campo requerido real."""
    context_mappings = {
        "table": [
            MappingEntry("fecha", "transaction_date", user_selected=False),
            MappingEntry("monto", "amount", user_selected=False),
            MappingEntry("notas", "notes", user_selected=False),
        ]
    }
    context_entities = {"table": "sale"}
    decisions = [
        ColumnRiskDecision(
            context_id="table",
            source_column="notas",
            target_field="amount",
            action="route_affected_rows_to_others",
        )
    ]

    violations = validate_column_risk_decisions(decisions, context_mappings, context_entities)

    assert len(violations) == 1
    assert violations[0].source_column == "notas"
    assert violations[0].target_field == "amount"


def test_decision_sobre_columna_no_mapeada_no_es_explicitly_selected() -> None:
    """Si la decisión referencia una columna que no aparece en el mapeo
    efectivo (caso raro, payload inconsistente), no hay `user_selected` que
    la salve: un target opcional sin match en el mapeo se trata como
    `optional` (no accionable) → route es violación."""
    context_mappings: dict[str, list[MappingEntry]] = {"table": []}
    context_entities = {"table": "product"}
    decisions = [
        ColumnRiskDecision(
            context_id="table",
            source_column="notas",
            target_field="notes",
            action="route_affected_rows_to_others",
        )
    ]

    violations = validate_column_risk_decisions(decisions, context_mappings, context_entities)

    assert len(violations) == 1
