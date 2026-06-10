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
        has_otros = bool(parsed_summary.get("otros_detectados"))

        if inferred == "ventas" and has_ventas:
            return DataIntentResult(True, "sale", confidence, "file")
        if inferred == "gastos" and has_gastos:
            return DataIntentResult(True, "expense", confidence, "file")
        if inferred == "stock" and has_stock:
            return DataIntentResult(True, "product", confidence, "file")
        if has_ventas and has_gastos:
            return DataIntentResult(True, "mixed", confidence, "file")
        # Archivos sin tipo inferido pero con datos (general) → tratar como ventas por defecto
        if inferred == "general" and has_ventas:
            return DataIntentResult(True, "sale", "LOW", "file")
        if inferred == "general" and has_gastos:
            return DataIntentResult(True, "expense", "LOW", "file")
        if inferred == "general" and has_stock:
            return DataIntentResult(True, "product", "LOW", "file")
        # FASE F: archivos ambiguos viven en otros_detectados. Hay datos
        # importables — el tipo lo decide el agente al que el CEO ruteó el
        # mensaje (el usuario dijo "son ventas"/"son gastos").
        if inferred == "general" and has_otros:
            return DataIntentResult(True, "unclassified", "LOW", "file")

        return DataIntentResult(False, None, "LOW", "file")
