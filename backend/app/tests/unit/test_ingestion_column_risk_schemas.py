"""F8b Task 1 — tests de los schemas de decisión de riesgo de columnas.

``ColumnRiskDecision`` es el contrato que el frontend manda dentro de
``ConfirmIngestionRequest`` para resolver una columna riesgosa (F8a) al
confirmar la importación. Estos tests son puros de schema (sin DB, sin
cliente HTTP): validan el contrato Pydantic, no la lógica de aplicación
(que llega en tasks posteriores de F8b).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.ingestion import ColumnRiskDecision, ConfirmIngestionRequest


class TestColumnRiskDecision:
    def test_acepta_drop_column(self) -> None:
        decision = ColumnRiskDecision(
            context_id="table",
            source_column="fecha_vencimiento",
            target_field="due_date",
            action="drop_column",
        )
        assert decision.action == "drop_column"

    def test_acepta_route_affected_rows_to_others(self) -> None:
        decision = ColumnRiskDecision(
            context_id="table",
            source_column="monto",
            target_field="amount",
            action="route_affected_rows_to_others",
        )
        assert decision.action == "route_affected_rows_to_others"

    def test_rechaza_action_fuera_del_literal(self) -> None:
        with pytest.raises(ValidationError):
            ColumnRiskDecision(
                context_id="table",
                source_column="monto",
                target_field="amount",
                action="cancel_and_complete",
            )

    def test_rechaza_action_arbitraria(self) -> None:
        with pytest.raises(ValidationError):
            ColumnRiskDecision(
                context_id="table",
                source_column="monto",
                target_field="amount",
                action="delete_everything",
            )


class TestConfirmIngestionRequestBackCompat:
    def test_sin_column_risk_decisions_valida_con_lista_vacia(self) -> None:
        """Confirms F7 (pre-F8b), sin el campo nuevo, siguen validando OK."""
        request = ConfirmIngestionRequest(confirmed_fields={"ventas": True})
        assert request.column_risk_decisions == []

    def test_con_column_risk_decisions_explicitas(self) -> None:
        request = ConfirmIngestionRequest(
            confirmed_fields={"ventas": True},
            column_risk_decisions=[
                ColumnRiskDecision(
                    context_id="table",
                    source_column="fecha_vencimiento",
                    target_field="due_date",
                    action="drop_column",
                )
            ],
        )
        assert len(request.column_risk_decisions) == 1
        assert request.column_risk_decisions[0].action == "drop_column"

    def test_dos_instancias_no_comparten_la_lista_default(self) -> None:
        r1 = ConfirmIngestionRequest(confirmed_fields={"ventas": True})
        r2 = ConfirmIngestionRequest(confirmed_fields={"gastos": True})
        r1.column_risk_decisions.append(
            ColumnRiskDecision(
                context_id="table",
                source_column="monto",
                target_field="amount",
                action="drop_column",
            )
        )
        assert r2.column_risk_decisions == []
