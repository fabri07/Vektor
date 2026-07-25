"""F8b (Task 4) — tests de la aplicación PURA de decisiones sobre una COPIA
del summary (`apply_column_risk_decisions` + `affected_rows_for_context`).

Fijan el contrato de la mutación sobre copia (sin DB):

- ``drop_column`` saca la columna de las filas / headers / preview_rows /
  columns_at_risk SOLO de su contexto (nunca de otro);
- ``route_affected_rows_to_others`` RECALCULA las filas afectadas (vacías o
  inválidas por el parser canónico) — nunca confía en un conteo del cliente —,
  las saca del bucket y las devuelve agrupadas por fila; las válidas quedan;
- el summary original (ORM-tracked) NUNCA se muta (se trabaja sobre copia).
"""

from __future__ import annotations

from typing import Any

from app.application.services.column_risk import (
    affected_rows_for_context,
    apply_column_risk_decisions,
)
from app.schemas.ingestion import ColumnRiskDecision


def _decision(cid: str, col: str, target: str, action: str) -> ColumnRiskDecision:
    return ColumnRiskDecision(
        context_id=cid, source_column=col, target_field=target, action=action
    )


# ── affected_rows_for_context (recálculo exacto) ─────────────────────────────


def test_affected_rows_recomputa_vacios_e_invalidos_no_los_validos() -> None:
    """amount: fila vacía y fila no-numérica son afectadas; la válida NO."""
    rows: list[dict[str, Any]] = [
        {"monto": "50000"},  # válida → importa
        {"monto": ""},  # vacía → afectada
        {"monto": "abc"},  # inválida (no numérica) → afectada
        {"monto": "0"},  # <=0 → _parse_amount la descarta → afectada
    ]
    affected = affected_rows_for_context(rows, [("monto", "amount")])
    assert set(affected) == {1, 2, 3}
    assert affected[1] == {"monto": ""}
    assert affected[2] == {"monto": "abc"}
    assert affected[3] == {"monto": "0"}


def test_affected_rows_combina_varias_columnas_por_fila() -> None:
    """Dos columnas ruteadas que afectan la MISMA fila → una sola entrada con
    ambos campos malos combinados (invariante 6)."""
    rows: list[dict[str, Any]] = [
        {"monto": "", "fecha": "no-es-fecha"},
        {"monto": "1000", "fecha": "2024-01-15"},
    ]
    affected = affected_rows_for_context(
        rows, [("monto", "amount"), ("fecha", "transaction_date")]
    )
    assert set(affected) == {0}
    assert affected[0] == {"monto": "", "fecha": "no-es-fecha"}


# ── apply_column_risk_decisions: drop_column ─────────────────────────────────


def test_drop_column_saca_columna_solo_de_su_contexto() -> None:
    summary: dict[str, Any] = {
        "file_type": "spreadsheet",
        "multi_sheet": True,
        "mapping_contexts": [
            {
                "context_id": "s1",
                "entity_type": "sale",
                "headers": ["fecha", "monto", "notas"],
                "preview_rows": [{"fecha": "x", "monto": "1", "notas": "a"}],
            },
            {
                "context_id": "s2",
                "entity_type": "expense",
                "headers": ["fecha", "monto", "notas"],
                "preview_rows": [{"fecha": "y", "monto": "2", "notas": "b"}],
            },
        ],
        "ventas_detectadas": [
            {"fecha": "2024-01-15", "monto": "1000", "notas": "hola", "__context__": "s1"}
        ],
        "gastos_detectados": [
            {"fecha": "2024-01-16", "monto": "500", "notas": "chau", "__context__": "s2"}
        ],
        "columns_at_risk": [{"column": "notas", "null_pct": 0.9}],
    }
    applied = apply_column_risk_decisions(
        summary,
        [_decision("s1", "notas", "notes", "drop_column")],
        {"s1": "sale", "s2": "expense"},
    )
    out = applied.summary
    # s1 pierde "notas" en fila, headers y preview
    assert "notas" not in out["ventas_detectadas"][0]
    s1 = next(c for c in out["mapping_contexts"] if c["context_id"] == "s1")
    assert s1["headers"] == ["fecha", "monto"]
    assert "notas" not in s1["preview_rows"][0]
    # s2 conserva "notas" (otro contexto, intacto)
    assert out["gastos_detectados"][0]["notas"] == "chau"
    s2 = next(c for c in out["mapping_contexts"] if c["context_id"] == "s2")
    assert s2["headers"] == ["fecha", "monto", "notas"]
    # columns_at_risk pierde la columna dropeada
    assert out["columns_at_risk"] == []
    assert applied.dropped_columns == {"s1": ["notas"]}
    # El summary ORIGINAL no se tocó (copia profunda)
    assert summary["ventas_detectadas"][0]["notas"] == "hola"
    assert summary["columns_at_risk"] == [{"column": "notas", "null_pct": 0.9}]


def test_drop_column_single_sheet_table_toca_headers_top_level() -> None:
    summary: dict[str, Any] = {
        "file_type": "spreadsheet",
        "inferred_type": "ventas",
        "headers": ["fecha", "monto", "notas"],
        "preview_rows": [{"fecha": "x", "monto": "1", "notas": "a"}],
        "ventas_detectadas": [{"fecha": "2024-01-15", "monto": "1000", "notas": "hola"}],
        "row_count": 1,
    }
    applied = apply_column_risk_decisions(
        summary,
        [_decision("table", "notas", "notes", "drop_column")],
        {"table": "sale"},
    )
    out = applied.summary
    assert out["headers"] == ["fecha", "monto"]
    assert "notas" not in out["preview_rows"][0]
    assert "notas" not in out["ventas_detectadas"][0]


# ── apply_column_risk_decisions: route ───────────────────────────────────────


def test_route_saca_solo_las_filas_afectadas_del_bucket() -> None:
    summary: dict[str, Any] = {
        "file_type": "spreadsheet",
        "inferred_type": "ventas",
        "ventas_detectadas": [
            {"fecha": "2024-01-15", "monto": "1000"},  # 0 válida → queda
            {"fecha": "2024-01-16", "monto": ""},  # 1 vacía → Otros
            {"fecha": "2024-01-17", "monto": "abc"},  # 2 inválida → Otros
        ],
        "row_count": 3,
    }
    applied = apply_column_risk_decisions(
        summary,
        [_decision("table", "monto", "amount", "route_affected_rows_to_others")],
        {"table": "sale"},
    )
    out = applied.summary
    # solo la fila válida sobrevive en el bucket
    assert len(out["ventas_detectadas"]) == 1
    assert out["ventas_detectadas"][0]["monto"] == "1000"
    # las afectadas se devuelven agrupadas por índice de contexto
    assert set(applied.routed_rows["table"]) == {1, 2}
    assert applied.routed_rows["table"][1] == {"monto": ""}
    assert applied.routed_rows["table"][2] == {"monto": "abc"}
    assert applied.routed_totals["table"] == 3
    assert applied.routed_entity["table"] == "sale"
    # original intacto
    assert len(summary["ventas_detectadas"]) == 3


def test_route_no_toca_otro_contexto() -> None:
    summary: dict[str, Any] = {
        "file_type": "spreadsheet",
        "multi_sheet": True,
        "mapping_contexts": [
            {"context_id": "s1", "entity_type": "sale", "headers": ["monto"]},
            {"context_id": "s2", "entity_type": "sale", "headers": ["monto"]},
        ],
        "ventas_detectadas": [
            {"monto": "", "__context__": "s1"},  # afectada en s1
            {"monto": "", "__context__": "s2"},  # afectada pero s2 NO se rutea
        ],
    }
    applied = apply_column_risk_decisions(
        summary,
        [_decision("s1", "monto", "amount", "route_affected_rows_to_others")],
        {"s1": "sale", "s2": "sale"},
    )
    out = applied.summary
    # s1 sacó su fila; s2 sigue con la suya
    remaining = out["ventas_detectadas"]
    assert len(remaining) == 1
    assert remaining[0]["__context__"] == "s2"
    assert set(applied.routed_rows["s1"]) == {0}
