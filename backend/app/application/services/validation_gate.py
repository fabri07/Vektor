"""ValidationGate — puerta de calidad del pipeline de ingestión.

Ejecutar ANTES de pasar parsed_summary_json al estado NEEDS_CONFIRMATION.
Si el archivo no supera la validación, retorna passed=False y el worker
debe marcar el archivo como REJECTED, nunca llegar al agente.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from app.observability.logger import get_logger

logger = get_logger(__name__)

_AMOUNT_CEILING = Decimal("50_000_000")  # 50M ARS — por encima es sospechoso de error de escala


@dataclass
class ValidationResult:
    passed: bool
    rejection_reason: str | None = None   # None si passed=True
    corrected_summary: dict | None = None  # None si not passed; dict con warnings si passed con correcciones


def _extract_amounts(summary: dict[str, Any]) -> list[Decimal]:
    """Extrae todos los montos numéricos del summary para sanity-check."""
    amounts: list[Decimal] = []
    file_type = summary.get("file_type", "spreadsheet")

    rows: list[Any] = []
    if file_type == "spreadsheet":
        rows = summary.get("ventas_detectadas", []) + summary.get("gastos_detectados", [])
    else:
        for entry in summary.get("ventas_detectadas", []) + summary.get("gastos_detectados", []):
            for raw in entry.get("montos", []):
                cleaned = re.sub(r"[$\s.]", "", str(raw)).replace(",", ".")
                try:
                    amounts.append(Decimal(cleaned))
                except InvalidOperation:
                    pass
        return amounts

    for row in rows:
        if not isinstance(row, dict):
            continue
        for v in row.values():
            if v is None:
                continue
            cleaned = re.sub(r"[$\s.]", "", str(v)).replace(",", ".")
            try:
                val = Decimal(cleaned)
                # Include negatives so the sanity check can catch them
                if val != 0:
                    amounts.append(val)
            except InvalidOperation:
                pass

    return amounts


def _extract_dates(summary: dict[str, Any]) -> tuple[list[date], int]:
    """
    Extrae fechas parseables del summary.
    Retorna (fechas_validas, total_valores_fecha) para detectar "todas inválidas".
    total_valores_fecha > 0 y fechas_validas == [] significa que había fechas pero ninguna parseable.
    """
    valid_dates: list[date] = []
    total_date_values = 0
    file_type = summary.get("file_type", "spreadsheet")

    if file_type != "spreadsheet":
        return valid_dates, total_date_values

    rows = summary.get("ventas_detectadas", []) + summary.get("gastos_detectados", [])
    for row in rows:
        if not isinstance(row, dict):
            continue
        for v in row.values():
            if v is None or not isinstance(v, str) or not v.strip():
                continue
            total_date_values += 1
            for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%y"):
                try:
                    valid_dates.append(datetime.strptime(v.strip(), fmt).date())
                    total_date_values -= 1  # no cuenta como "fallido" si parsea
                    break
                except ValueError:
                    continue

    return valid_dates, total_date_values


class ValidationGate:
    """
    Aplica 4 reglas de calidad sobre el parsed_summary_json de un archivo ingestado.

    Uso:
        gate = ValidationGate()
        result = gate.validate(parsed_summary, force=False)
        if not result.passed:
            # marcar archivo REJECTED
    """

    def validate(self, parsed_summary: dict[str, Any], force: bool = False) -> ValidationResult:
        """
        Ejecuta todas las reglas en orden. La primera que falla rechaza el archivo.
        Si force=True, la Regla 1 (confidence=LOW) se registra como warning pero no rechaza.
        """
        warnings: list[str] = []

        # ── Regla 1: Confidence mínima ────────────────────────────────────────
        confidence = parsed_summary.get("confidence", "MEDIUM")
        if confidence == "LOW":
            if force:
                logger.warning(
                    "validation_gate.low_confidence_forced",
                    confidence=confidence,
                )
                warnings.append("confidence_low_forced_by_user")
            else:
                logger.info("validation_gate.rejected", reason="confidence_too_low")
                return ValidationResult(passed=False, rejection_reason="confidence_too_low")

        # ── Regla 2: Schema mínimo según inferred_type ────────────────────────
        inferred_type = parsed_summary.get("inferred_type", "general")
        schema_result = self._check_schema(parsed_summary, inferred_type)
        if schema_result is not None:
            logger.info("validation_gate.rejected", reason=schema_result)
            return ValidationResult(passed=False, rejection_reason=schema_result)

        # ── Regla 3: Sanity check de montos ───────────────────────────────────
        amounts = _extract_amounts(parsed_summary)
        for amt in amounts:
            if amt < 0:
                logger.info("validation_gate.rejected", reason="negative_amount")
                return ValidationResult(passed=False, rejection_reason="negative_amount")
            if amt > _AMOUNT_CEILING:
                warnings.append(f"amount_scale_warning:{amt}")
                logger.warning(
                    "validation_gate.amount_scale_warning",
                    amount=str(amt),
                    ceiling=str(_AMOUNT_CEILING),
                )

        # ── Regla 4: Fechas coherentes ────────────────────────────────────────
        dates, failed_date_count = _extract_dates(parsed_summary)
        # Si había columnas con valores de fecha pero NINGUNO se pudo parsear → rechazar
        if failed_date_count > 0 and not dates and parsed_summary.get("has_fecha"):
            logger.info("validation_gate.rejected", reason="all_dates_invalid")
            return ValidationResult(passed=False, rejection_reason="all_dates_invalid")

        if dates:
            today = date.today()
            future_limit = today + timedelta(days=1)
            past_limit = today - timedelta(days=730)

            for d in dates:
                if d > future_limit:
                    warnings.append(f"future_date_warning:{d.isoformat()}")
                if d < past_limit:
                    warnings.append(f"old_date_warning:{d.isoformat()}")

        # ── Pasó todas las reglas ─────────────────────────────────────────────
        if warnings:
            corrected = dict(parsed_summary)
            existing_warnings = list(corrected.get("warnings") or [])
            corrected["warnings"] = existing_warnings + warnings
            return ValidationResult(passed=True, corrected_summary=corrected)

        return ValidationResult(passed=True)

    @staticmethod
    def _check_schema(summary: dict[str, Any], inferred_type: str) -> str | None:
        """
        Retorna un rejection_reason string si el schema es insuficiente, None si pasa.
        """
        file_type = summary.get("file_type", "spreadsheet")

        if inferred_type == "ventas":
            if file_type == "spreadsheet":
                rows = summary.get("ventas_detectadas", [])
                if not rows:
                    return "schema_ventas_empty"
                if not summary.get("has_venta"):
                    return "schema_ventas_no_amount_col"
            else:
                ventas = summary.get("ventas_detectadas", [])
                if not any(entry.get("montos") for entry in ventas):
                    return "schema_ventas_no_amounts_detected"

        elif inferred_type == "gastos":
            if file_type == "spreadsheet":
                rows = summary.get("gastos_detectados", [])
                if not rows:
                    return "schema_gastos_empty"
                if not summary.get("has_gasto"):
                    return "schema_gastos_no_amount_col"
            else:
                gastos = summary.get("gastos_detectados", [])
                if not any(entry.get("montos") for entry in gastos):
                    return "schema_gastos_no_amounts_detected"

        elif inferred_type == "stock":
            stock = summary.get("stock_detectado", [])
            if not stock:
                return "schema_stock_empty"

        elif inferred_type == "general":
            has_any = (
                summary.get("has_venta")
                or summary.get("has_gasto")
                or summary.get("has_producto")
                or bool(summary.get("ventas_detectadas"))
                or bool(summary.get("gastos_detectados"))
                or bool(summary.get("stock_detectado"))
            )
            if not has_any:
                return "schema_general_no_data_detected"

        return None
