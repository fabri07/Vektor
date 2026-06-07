"""FASE 2: 4ª capa LLM de mapeo de columnas (fallback)."""

from __future__ import annotations

import unittest.mock
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.application.services import llm_column_mapper
from app.application.services.column_mapping_service import ColumnMappingService
from app.application.services.llm_column_mapper import _parse_llm_json, suggest_with_llm

# ── _parse_llm_json ───────────────────────────────────────────────────────────


def test_parse_valid_json() -> None:
    raw = '{"Col A": {"target_field": "amount", "confidence": 0.9}}'
    out = _parse_llm_json(raw, {"amount", "transaction_date"})
    assert out == {"Col A": {"target_field": "amount", "confidence": 0.9}}


def test_parse_tolerates_code_fences() -> None:
    raw = '```json\n{"X": {"target_field": "ignore", "confidence": 0.4}}\n```'
    out = _parse_llm_json(raw, {"amount"})
    assert out["X"]["target_field"] == "ignore"


def test_parse_drops_invalid_target() -> None:
    raw = '{"X": {"target_field": "campo_inventado", "confidence": 1.0}}'
    assert _parse_llm_json(raw, {"amount"}) == {}


def test_parse_clamps_confidence() -> None:
    raw = '{"X": {"target_field": "amount", "confidence": 5}}'
    assert _parse_llm_json(raw, {"amount"})["X"]["confidence"] == 1.0


def test_parse_garbage_returns_empty() -> None:
    assert _parse_llm_json("no soy json", {"amount"}) == {}
    assert _parse_llm_json("[1, 2, 3]", {"amount"}) == {}


# ── suggest_with_llm: flag gating ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_llm_disabled_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    # Flag apagado (default) → no llama al LLM.
    cols = [{"header": "X", "sample_values": ["1"]}]
    out = await suggest_with_llm("sale", cols, {"amount": "Monto"})
    assert out == {}


@pytest.mark.asyncio
async def test_llm_no_columns_returns_empty() -> None:
    assert await suggest_with_llm("sale", [], {"amount": "Monto"}) == {}


# ── Integración: suggest_mappings usa el LLM solo en columnas dudosas ─────────


@pytest.mark.asyncio
async def test_suggest_mappings_applies_llm_to_low_confidence() -> None:
    db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []  # sin historial
    db.execute = AsyncMock(return_value=mock_result)

    svc = ColumnMappingService(db)
    # "Fecha" matchea heurística (alta confianza, NO va al LLM).
    # "ColRara99" no matchea nada (baja confianza → va al LLM).
    headers = ["Fecha", "ColRara99"]
    sample_rows = [{"Fecha": "2024-01-15", "ColRara99": "1500"}]

    async def _fake_llm(
        entity_type: str, columns: list[dict[str, Any]], valid_fields: dict[str, str]
    ) -> dict[str, dict[str, Any]]:
        # El LLM solo debe recibir la columna dudosa, no "Fecha".
        assert [c["header"] for c in columns] == ["ColRara99"]
        return {"ColRara99": {"target_field": "amount", "confidence": 0.88}}

    with unittest.mock.patch.object(
        llm_column_mapper, "suggest_with_llm", side_effect=_fake_llm
    ):
        suggestions = await svc.suggest_mappings(uuid.uuid4(), "sale", headers, sample_rows)

    fecha = next(s for s in suggestions if s["source_column"] == "Fecha")
    assert fecha["source"] == "heuristic"  # NO tocada por el LLM

    rara = next(s for s in suggestions if s["source_column"] == "ColRara99")
    assert rara["source"] == "llm"
    assert rara["target_field"] == "amount"
    assert rara["status"] == "mapped"
    assert rara["confidence"] == 0.88


@pytest.mark.asyncio
async def test_llm_ignore_does_not_override() -> None:
    db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=mock_result)
    svc = ColumnMappingService(db)

    async def _fake_llm(*_a: Any, **_k: Any) -> dict[str, dict[str, Any]]:
        return {"ColRara99": {"target_field": "ignore", "confidence": 0.95}}

    with unittest.mock.patch.object(
        llm_column_mapper, "suggest_with_llm", side_effect=_fake_llm
    ):
        suggestions = await svc.suggest_mappings(
            uuid.uuid4(), "sale", ["ColRara99"], [{"ColRara99": "x"}]
        )

    rara = suggestions[0]
    assert rara["source"] != "llm"  # "ignore" no pisa el resultado determinístico
    assert rara["status"] == "unmapped"
