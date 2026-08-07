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
    """Un target opcional que existe en el mapeo pero SIN `user_selected` no
    tiene ningún `user_selected` que lo salve: se trata como `optional` (no
    accionable) → route es violación. `matched_entry` SÍ existe acá (la
    columna está realmente mapeada) para aislar este branch del de
    `matched_entry is None` (ver `test_decision_con_source_column_falso_...`),
    que intercepta antes si la columna no está en el mapeo en absoluto."""
    context_mappings: dict[str, list[MappingEntry]] = {
        "table": [MappingEntry("notas", "notes", user_selected=False)]
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


# ── F-H4: el monto tiene alternativa, y las dos validaciones tienen que saberlo ──


def test_drop_del_monto_con_precio_y_cantidad_mapeados_ok() -> None:
    """Eliminar la columna del monto es legal si queda con qué calcularlo.

    Es el caso que motiva F-H4 y no una hipótesis: una columna de monto casi toda
    vacía al lado de precio unitario y cantidad completos es exactamente lo que
    dispara el protocolo de riesgo. Sin esta regla, el confirm aceptaría la hoja
    sin monto mapeado pero eliminarlo daría 422 — dos validaciones diciendo cosas
    distintas sobre el mismo archivo.
    """
    context_mappings = {
        "table": [
            MappingEntry("fecha", "transaction_date", user_selected=False),
            MappingEntry("monto", "amount", user_selected=False),
            MappingEntry("p_unit", "unit_price", user_selected=True),
            MappingEntry("cant", "quantity", user_selected=True),
        ]
    }
    decisions = [
        ColumnRiskDecision(
            context_id="table",
            source_column="monto",
            target_field="amount",
            action="drop_column",
        )
    ]

    violations = validate_column_risk_decisions(
        decisions, context_mappings, {"table": "sale"}
    )

    assert violations == []


def test_drop_del_monto_con_media_alternativa_es_violacion() -> None:
    """Control: sin la cantidad no hay nada que calcular, así que el monto sigue
    siendo obligatorio y eliminarlo deja la hoja sin importar."""
    context_mappings = {
        "table": [
            MappingEntry("fecha", "transaction_date", user_selected=False),
            MappingEntry("monto", "amount", user_selected=False),
            MappingEntry("p_unit", "unit_price", user_selected=True),
        ]
    }
    decisions = [
        ColumnRiskDecision(
            context_id="table",
            source_column="monto",
            target_field="amount",
            action="drop_column",
        )
    ]

    violations = validate_column_risk_decisions(
        decisions, context_mappings, {"table": "sale"}
    )

    assert len(violations) == 1
    assert violations[0].target_field == "amount"


def test_drop_del_monto_y_de_la_alternativa_en_el_mismo_pedido_es_violacion() -> None:
    """La alternativa se evalúa sobre lo que va a QUEDAR, no sobre lo que llegó:
    eliminar el monto y la cantidad en el mismo batch deja la hoja sin ninguna
    de las dos vías."""
    context_mappings = {
        "table": [
            MappingEntry("fecha", "transaction_date", user_selected=False),
            MappingEntry("monto", "amount", user_selected=False),
            MappingEntry("p_unit", "unit_price", user_selected=True),
            MappingEntry("cant", "quantity", user_selected=True),
        ]
    }
    decisions = [
        ColumnRiskDecision(
            context_id="table",
            source_column="monto",
            target_field="amount",
            action="drop_column",
        ),
        ColumnRiskDecision(
            context_id="table",
            source_column="cant",
            target_field="quantity",
            action="drop_column",
        ),
    ]

    violations = validate_column_risk_decisions(
        decisions, context_mappings, {"table": "sale"}
    )

    assert [v.target_field for v in violations] == ["amount"]


def test_el_gasto_no_tiene_alternativa_para_su_monto() -> None:
    """Gastos y compras quedan afuera hasta F-H6: hoy no tienen `unit_price` ni
    `quantity` en su catálogo, así que derivar sería adivinar desde columnas que
    nadie declaró."""
    context_mappings = {
        "table": [
            MappingEntry("fecha", "expense_date", user_selected=False),
            MappingEntry("monto", "amount", user_selected=False),
            MappingEntry("p_unit", "unit_price", user_selected=True),
            MappingEntry("cant", "quantity", user_selected=True),
        ]
    }
    decisions = [
        ColumnRiskDecision(
            context_id="table",
            source_column="monto",
            target_field="amount",
            action="drop_column",
        )
    ]

    violations = validate_column_risk_decisions(
        decisions, context_mappings, {"table": "expense"}
    )

    assert len(violations) == 1
