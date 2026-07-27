"""F8a — tests del contrato de riesgo contextual de columnas (`column_risk`).

El helper `build_contextual_column_risk` es PURO (sin DB, sin LLM): recibe un
summary parseado + el mapeo efectivo por contexto y devuelve la lista de riesgos
contextuales. Estos tests fijan las reglas del contrato:

- un opcional automapeado con muchos nulos NO es accionable;
- un requerido con muchos nulos SÍ aparece y es accionable;
- un opcional que el usuario seleccionó explícitamente SÍ es accionable;
- misma columna en dos contextos NO mezcla conteos;
- `affected_rows` es EXACTO (nulos + inválidos), nunca round(ratio*row_count);
- summary legacy sin `mapping_contexts` no rompe.
"""

from __future__ import annotations

from typing import Any

from app.application.services.column_risk import (
    MappingEntry,
    build_contextual_column_risk,
    split_derivable_decisions,
)


def _ctx(context_id: str, entity_type: str, headers: list[str]) -> dict[str, Any]:
    return {
        "context_id": context_id,
        "label": context_id,
        "source_kind": "sheet",
        "entity_type": entity_type,
        "headers": headers,
        "fields": None,
        "preview_rows": [],
        "row_count": 0,
    }


def _find(
    risks: list[dict[str, Any]], context_id: str, source_column: str
) -> dict[str, Any] | None:
    for r in risks:
        if r["context_id"] == context_id and r["source_column"] == source_column:
            return r
    return None


def test_optional_automapeado_alto_null_no_es_accionable() -> None:
    """Un opcional (notes) automapeado con 90% de nulos: informativo, no accionable."""
    rows = [{"nombre": f"P{i}", "notas": (None if i > 0 else "hola")} for i in range(10)]
    summary = {
        "mapping_contexts": [_ctx("table", "product", ["nombre", "notas"])],
        "stock_detectado": rows,
    }
    mappings = {
        "table": [
            MappingEntry("nombre", "name", mapping_source="heuristic", user_selected=False),
            MappingEntry("notas", "notes", mapping_source="heuristic", user_selected=False),
        ]
    }
    risks = build_contextual_column_risk(summary, mappings)
    notas = _find(risks, "table", "notas")
    assert notas is not None  # se surface como high_null_ratio informativo
    assert notas["field_requirement"] == "optional"
    assert notas["allowed_actions"] == []  # NO accionable


def test_requerido_alto_null_es_accionable() -> None:
    """transaction_date (requerido) con muchos nulos aparece y permite rutear."""
    rows = [{"fecha": (None if i > 1 else "01/02/2026"), "monto": "100"} for i in range(10)]
    summary = {
        "mapping_contexts": [_ctx("table", "sale", ["fecha", "monto"])],
        "ventas_detectadas": rows,
    }
    mappings = {
        "table": [
            MappingEntry("fecha", "transaction_date", mapping_source="heuristic"),
            MappingEntry("monto", "amount", mapping_source="heuristic"),
        ]
    }
    risks = build_contextual_column_risk(summary, mappings)
    fecha = _find(risks, "table", "fecha")
    assert fecha is not None
    assert fecha["field_requirement"] == "required"
    assert "route_affected_rows_to_others" in fecha["allowed_actions"]
    # requerido con una sola columna mapeada: NO se puede dropear (dejaría sin fecha)
    assert "drop_column" not in fecha["allowed_actions"]


def test_opcional_seleccionado_por_usuario_es_accionable() -> None:
    """Un opcional (category) con user_selected=True se vuelve explicitly_selected."""
    rows = [
        {"cat": (None if i > 2 else "Bebidas"), "monto": "50", "fecha": "01/02/2026"}
        for i in range(10)
    ]
    summary = {
        "mapping_contexts": [_ctx("table", "expense", ["cat", "monto", "fecha"])],
        "gastos_detectados": rows,
    }
    mappings = {
        "table": [
            MappingEntry("cat", "category", mapping_source="fuzzy", user_selected=True),
            MappingEntry("monto", "amount"),
            MappingEntry("fecha", "expense_date"),
        ]
    }
    risks = build_contextual_column_risk(summary, mappings)
    cat = _find(risks, "table", "cat")
    assert cat is not None
    assert cat["field_requirement"] == "explicitly_selected"
    assert "drop_column" in cat["allowed_actions"]
    assert "route_affected_rows_to_others" in cat["allowed_actions"]


def test_misma_columna_dos_contextos_no_se_mezcla() -> None:
    """'fecha' en dos hojas: conteos independientes por context_id."""
    enero = [
        {"fecha": (None if i > 0 else "01/01/2026"), "monto": "10", "__context__": "sheet:Enero"}
        for i in range(4)
    ]
    febrero = [
        {"fecha": "01/02/2026", "monto": "20", "__context__": "sheet:Febrero"} for _ in range(4)
    ]
    summary = {
        "mapping_contexts": [
            _ctx("sheet:Enero", "sale", ["fecha", "monto"]),
            _ctx("sheet:Febrero", "sale", ["fecha", "monto"]),
        ],
        "ventas_detectadas": enero + febrero,
    }
    date_amount = [MappingEntry("fecha", "transaction_date"), MappingEntry("monto", "amount")]
    mappings = {"sheet:Enero": date_amount, "sheet:Febrero": date_amount}
    risks = build_contextual_column_risk(summary, mappings)
    enero_fecha = _find(risks, "sheet:Enero", "fecha")
    assert enero_fecha is not None
    assert enero_fecha["null_rows"] == 3  # 3 de 4 vacías en Enero
    # Febrero no tiene fechas vacías → sin riesgo de fecha
    assert _find(risks, "sheet:Febrero", "fecha") is None


def test_affected_rows_exacto_nulos_mas_invalidos() -> None:
    """5 filas: 2 fecha vacía + 1 fecha inválida → affected=3, NO round(ratio*n)."""
    rows = [
        {"fecha": "01/02/2026", "monto": "10"},
        {"fecha": "05/02/2026", "monto": "20"},
        {"fecha": None, "monto": "30"},
        {"fecha": "", "monto": "40"},
        {"fecha": "no-es-fecha", "monto": "50"},
    ]
    summary = {
        "mapping_contexts": [_ctx("table", "sale", ["fecha", "monto"])],
        "ventas_detectadas": rows,
    }
    mappings = {
        "table": [MappingEntry("fecha", "transaction_date"), MappingEntry("monto", "amount")],
    }
    risks = build_contextual_column_risk(summary, mappings)
    fecha = _find(risks, "table", "fecha")
    assert fecha is not None
    assert fecha["null_rows"] == 2
    assert fecha["invalid_rows"] == 1
    assert fecha["affected_rows"] == 3
    assert fecha["null_ratio"] == 0.4  # 2/5, exacto


def test_summary_legacy_sin_mapping_contexts_ni_headers_no_rompe() -> None:
    # Sin headers ni inferred_type no se puede sintetizar contexto → sin diagnóstico.
    summary = {"ventas_detectadas": [{"fecha": None, "monto": "10"}]}
    mappings = {"table": [MappingEntry("fecha", "transaction_date")]}
    risks = build_contextual_column_risk(summary, mappings)
    assert risks == []


def test_legacy_fallback_sintetiza_contexto_table_desde_inferred_type() -> None:
    """Summary legacy sin mapping_contexts pero con inferred_type+headers → contexto
    sintético 'table' con diagnóstico F8 (fallback prometido en el plan)."""
    rows = [{"fecha": None, "monto": "100"} for _ in range(6)]
    summary = {
        "inferred_type": "ventas",
        "headers": ["fecha", "monto"],
        "row_count": 6,
        "ventas_detectadas": rows,
    }
    mappings = {"table": [MappingEntry("fecha", "transaction_date")]}
    risks = build_contextual_column_risk(summary, mappings)
    fecha = _find(risks, "table", "fecha")
    assert fecha is not None
    assert fecha["field_requirement"] == "required"
    assert fecha["null_rows"] == 6


# ── Maestros (customer / supplier) ──────────────────────────────────────────────


def test_customer_name_requerido_con_vacios_es_accionable() -> None:
    rows = [{"nombre": (None if i > 1 else "Juan"), "tel": "111"} for i in range(6)]
    summary = {
        "mapping_contexts": [_ctx("table", "customer", ["nombre", "tel"])],
        "clientes_detectados": rows,
    }
    mappings = {"table": [MappingEntry("nombre", "name"), MappingEntry("tel", "phone")]}
    risks = build_contextual_column_risk(summary, mappings)
    name = _find(risks, "table", "nombre")
    assert name is not None
    assert name["field_requirement"] == "required"
    assert "route_affected_rows_to_others" in name["allowed_actions"]


def test_customer_telefono_opcional_vacio_no_es_accionable() -> None:
    rows = [{"nombre": "Juan", "tel": None} for _ in range(6)]  # tel 100% vacío, opcional
    summary = {
        "mapping_contexts": [_ctx("table", "customer", ["nombre", "tel"])],
        "clientes_detectados": rows,
    }
    mappings = {"table": [MappingEntry("nombre", "name"), MappingEntry("tel", "phone")]}
    risks = build_contextual_column_risk(summary, mappings)
    tel = _find(risks, "table", "tel")
    assert tel is not None  # high_null_ratio informativo
    assert tel["field_requirement"] == "optional"
    assert tel["allowed_actions"] == []


def test_supplier_name_requerido_aparece() -> None:
    rows = [{"razon": (None if i > 0 else "ACME"), "mail": "a@a.com"} for i in range(5)]
    summary = {
        "mapping_contexts": [_ctx("table", "supplier", ["razon", "mail"])],
        "proveedores_detectados": rows,
    }
    mappings = {"table": [MappingEntry("razon", "name"), MappingEntry("mail", "email")]}
    risks = build_contextual_column_risk(summary, mappings)
    name = _find(risks, "table", "razon")
    assert name is not None
    assert name["field_requirement"] == "required"


def test_supplier_opcional_seleccionado_por_usuario_es_accionable() -> None:
    rows = [{"razon": "ACME", "mail": (None if i > 2 else "a@a.com")} for i in range(6)]
    summary = {
        "mapping_contexts": [_ctx("table", "supplier", ["razon", "mail"])],
        "proveedores_detectados": rows,
    }
    mappings = {
        "table": [
            MappingEntry("razon", "name"),
            MappingEntry("mail", "email", user_selected=True),
        ]
    }
    risks = build_contextual_column_risk(summary, mappings)
    mail = _find(risks, "table", "mail")
    assert mail is not None
    assert mail["field_requirement"] == "explicitly_selected"
    assert "drop_column" in mail["allowed_actions"]


# ── Entidad efectiva (override de reasignación) ─────────────────────────────────


def test_context_entities_override_cambia_los_requeridos() -> None:
    """Una hoja detectada como product, reasignada a sale, hace transaction_date
    requerida (sin el override se usaría REQUIRED_FIELDS['product'] → equivocado)."""
    rows = [
        {"fecha": (None if i > 1 else "01/02/2026"), "monto": "100", "__context__": "sheet:X"}
        for i in range(6)
    ]
    summary = {
        "mapping_contexts": [_ctx("sheet:X", "product", ["fecha", "monto"])],
        "ventas_detectadas": rows,
    }
    mappings = {
        "sheet:X": [MappingEntry("fecha", "transaction_date"), MappingEntry("monto", "amount")]
    }
    # Sin override: entidad product → transaction_date NO es requerida.
    sin = _find(build_contextual_column_risk(summary, mappings), "sheet:X", "fecha")
    assert sin is None or sin["field_requirement"] == "optional"
    # Con override a sale → requerida.
    con = _find(
        build_contextual_column_risk(summary, mappings, context_entities={"sheet:X": "sale"}),
        "sheet:X",
        "fecha",
    )
    assert con is not None
    assert con["field_requirement"] == "required"


# ── Inclusión por contexto (misma decisión que el confirm) ──────────────────────


def test_contexto_excluido_no_genera_riesgo() -> None:
    rows = [{"fecha": None, "monto": "100"} for _ in range(6)]
    summary = {
        "mapping_contexts": [_ctx("table", "sale", ["fecha", "monto"])],
        "ventas_detectadas": rows,
    }
    mappings = {"table": [MappingEntry("fecha", "transaction_date")]}
    incluido = build_contextual_column_risk(summary, mappings, context_confirmed={"table": True})
    assert _find(incluido, "table", "fecha") is not None
    excluido = build_contextual_column_risk(summary, mappings, context_confirmed={"table": False})
    assert excluido == []
    # Legacy: gating por confirmed_fields.
    excluido_legacy = build_contextual_column_risk(
        summary, mappings, confirmed_fields={"ventas": False}
    )
    assert excluido_legacy == []


# ── Exactitud de validadores (reusa parsers reales del importador) ──────────────


def test_amount_no_positivo_o_garbage_cuenta_invalido() -> None:
    """`_parse_amount` descarta ≤0 y no numérico → cuentan como inválidos (exacto
    respecto del confirm, que no inserta esas filas)."""
    rows = [{"fecha": "01/02/2026", "monto": m} for m in ["100", "0", "-5", "200", "abc"]]
    summary = {
        "mapping_contexts": [_ctx("table", "sale", ["fecha", "monto"])],
        "ventas_detectadas": rows,
    }
    mappings = {
        "table": [MappingEntry("fecha", "transaction_date"), MappingEntry("monto", "amount")]
    }
    monto = _find(build_contextual_column_risk(summary, mappings), "table", "monto")
    assert monto is not None
    assert monto["null_rows"] == 0
    assert monto["invalid_rows"] == 3  # "0", "-5", "abc"
    assert monto["affected_rows"] == 3


def test_quantity_garbage_no_cuenta_porque_el_importador_coerce() -> None:
    """quantity/stock_units los coerce el importador (_parse_qty) → invalid_rows=0;
    solo la vaciedad cuenta."""
    rows = [
        {"cant": c, "monto": "100", "fecha": "01/02/2026"} for c in ["abc", "-3", "", "5"]
    ]
    summary = {
        "mapping_contexts": [_ctx("table", "sale", ["cant", "monto", "fecha"])],
        "ventas_detectadas": rows,
    }
    mappings = {
        "table": [
            MappingEntry("cant", "quantity", user_selected=True),
            MappingEntry("monto", "amount"),
            MappingEntry("fecha", "transaction_date"),
        ]
    }
    cant = _find(build_contextual_column_risk(summary, mappings), "table", "cant")
    assert cant is not None  # explicitly_selected + affected>0
    assert cant["invalid_rows"] == 0
    assert cant["null_rows"] == 1  # solo la celda vacía


# ── F8c: Separación de decisiones derivables (función pura) ────────────────────


def test_split_derivable_decisions_una_accion_forzada() -> None:
    """Una row con allowed_actions=[\"drop_column\"] → decisión forzada."""
    risk_rows = [
        {
            "context_id": "table",
            "source_column": "col1",
            "target_field": "field1",
            "allowed_actions": ["drop_column"],
        }
    ]
    forced, ambiguous = split_derivable_decisions(risk_rows)
    assert len(forced) == 1
    assert len(ambiguous) == 0
    assert forced[0].context_id == "table"
    assert forced[0].source_column == "col1"
    assert forced[0].target_field == "field1"
    assert forced[0].action == "drop_column"


def test_split_derivable_decisions_multiples_acciones_ambigua() -> None:
    """Una row con allowed_actions=[\"drop_column\", \"route_affected_rows_to_others\"]
    → ambigua."""
    risk_rows = [
        {
            "context_id": "table",
            "source_column": "col1",
            "target_field": "field1",
            "allowed_actions": ["drop_column", "route_affected_rows_to_others"],
        }
    ]
    forced, ambiguous = split_derivable_decisions(risk_rows)
    assert len(forced) == 0
    assert len(ambiguous) == 1
    assert ambiguous[0]["context_id"] == "table"
    assert ambiguous[0]["source_column"] == "col1"


def test_split_derivable_decisions_sin_acciones_ignorada() -> None:
    """Una row con allowed_actions=[] → ignorada (ni forzada ni ambigua)."""
    risk_rows = [
        {
            "context_id": "table",
            "source_column": "col1",
            "target_field": "field1",
            "allowed_actions": [],
        }
    ]
    forced, ambiguous = split_derivable_decisions(risk_rows)
    assert len(forced) == 0
    assert len(ambiguous) == 0


def test_split_derivable_decisions_mezcla_casos() -> None:
    """Mezcla de tres tipos: 1 forzada, 1 ambigua, 1 ignorada."""
    risk_rows = [
        {
            "context_id": "table",
            "source_column": "col1",
            "target_field": "field1",
            "allowed_actions": ["drop_column"],  # forzada
        },
        {
            "context_id": "table",
            "source_column": "col2",
            "target_field": "field2",
            "allowed_actions": ["drop_column", "route_affected_rows_to_others"],  # ambigua
        },
        {
            "context_id": "table",
            "source_column": "col3",
            "target_field": "field3",
            "allowed_actions": [],  # ignorada
        },
    ]
    forced, ambiguous = split_derivable_decisions(risk_rows)
    assert len(forced) == 1
    assert len(ambiguous) == 1
    assert forced[0].source_column == "col1"
    assert ambiguous[0]["source_column"] == "col2"
