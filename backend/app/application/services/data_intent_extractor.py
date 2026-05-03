"""Deterministic business-data intent detection for parsed files."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DataIntentResult:
    has_data_intent: bool
    intent_type: str | None
    confidence: str
    source: str


class DataIntentExtractor:
    """Detects whether a parsed file contains loadable business data without LLM calls."""

    def check_file_summary(self, parsed_summary: dict[str, Any]) -> DataIntentResult:
        inferred = parsed_summary.get("inferred_type", "general")
        confidence = parsed_summary.get("confidence", "LOW")

        has_ventas = bool(parsed_summary.get("ventas_detectadas"))
        has_gastos = bool(parsed_summary.get("gastos_detectados"))
        has_stock = bool(parsed_summary.get("stock_detectado"))

        if inferred == "ventas" and has_ventas:
            return DataIntentResult(True, "sale", confidence, "file")
        if inferred == "gastos" and has_gastos:
            return DataIntentResult(True, "expense", confidence, "file")
        if inferred == "stock" and has_stock:
            return DataIntentResult(True, "product", confidence, "file")
        if has_ventas and has_gastos:
            return DataIntentResult(True, "mixed", confidence, "file")

        return DataIntentResult(False, None, "LOW", "file")
