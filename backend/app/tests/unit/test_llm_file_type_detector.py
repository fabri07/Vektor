"""FASE 2 (A1): detección del propósito del archivo por contenido con LLM."""

from __future__ import annotations

import unittest.mock
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.application.services import llm_file_type_detector
from app.application.services.llm_file_type_detector import (
    _parse_llm_response,
    detect_file_type,
    maybe_detect_file_type,
)
from app.config.settings import get_settings

# ── _parse_llm_response ────────────────────────────────────────────────────────


def test_parse_valid() -> None:
    assert _parse_llm_response('{"file_type": "ventas", "confidence": 0.9}') == ("ventas", 0.9)


def test_parse_tolerates_fences() -> None:
    raw = '```json\n{"file_type": "gastos", "confidence": 0.7}\n```'
    assert _parse_llm_response(raw) == ("gastos", 0.7)


def test_parse_invalid_type_returns_none() -> None:
    # "mixto" no es un tipo válido → None (pero conserva confidence parseada).
    assert _parse_llm_response('{"file_type": "mixto", "confidence": 0.8}') == (None, 0.8)


def test_parse_general_returns_none() -> None:
    assert _parse_llm_response('{"file_type": "general", "confidence": 0.3}') == (None, 0.3)


def test_parse_clamps_confidence() -> None:
    assert _parse_llm_response('{"file_type": "stock", "confidence": 5}') == ("stock", 1.0)


def test_parse_garbage_returns_none() -> None:
    assert _parse_llm_response("no soy json") == (None, 0.0)
    assert _parse_llm_response("[1,2,3]") == (None, 0.0)


# ── detect_file_type: flag gating ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_detect_disabled_returns_none() -> None:
    # Flag apagado (default) → no llama al LLM.
    detected, conf, model = await detect_file_type(["fecha", "monto"], [{"fecha": "x"}])
    assert detected is None
    assert conf == 0.0
    assert model  # el modelo configurable siempre vuelve


@pytest.mark.asyncio
async def test_detect_no_headers_returns_none() -> None:
    assert (await detect_file_type([], []))[0] is None


@pytest.mark.asyncio
async def test_detect_enabled_with_mocked_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "ENABLE_LLM_FILE_TYPE_DETECTION", True)

    mock_msg = MagicMock()
    mock_msg.text = '{"file_type": "ventas", "confidence": 0.92}'
    mock_response = MagicMock()
    mock_response.content = [mock_msg]
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)

    with unittest.mock.patch(
        "app.integrations.anthropic_client.get_anthropic_async_client",
        return_value=mock_client,
    ):
        detected, conf, _ = await detect_file_type(
            ["cliente", "total"], [{"cliente": "Juan", "total": "1500"}]
        )

    assert detected == "ventas"
    assert conf == 0.92
    mock_client.messages.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_detect_failure_is_fail_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "ENABLE_LLM_FILE_TYPE_DETECTION", True)
    with unittest.mock.patch(
        "app.integrations.anthropic_client.get_anthropic_async_client",
        side_effect=RuntimeError("boom"),
    ):
        detected, conf, _ = await detect_file_type(["x"], [{"x": "1"}])
    assert detected is None


# ── maybe_detect_file_type: orquestación + traza ──────────────────────────────


@pytest.mark.asyncio
async def test_maybe_detect_skips_non_general() -> None:
    summary = {"inferred_type": "ventas", "headers": ["a"], "preview_rows": [{"a": "1"}]}
    db = AsyncMock()
    with unittest.mock.patch.object(
        llm_file_type_detector, "detect_file_type", new=AsyncMock()
    ) as spy:
        await maybe_detect_file_type(
            db, summary, trace_id=uuid.uuid4(), tenant_id=uuid.uuid4(), file_id=uuid.uuid4()
        )
    spy.assert_not_awaited()
    assert summary["inferred_type"] == "ventas"  # intacto


@pytest.mark.asyncio
async def test_maybe_detect_updates_and_emits() -> None:
    summary: dict[str, Any] = {
        "inferred_type": "general",
        "headers": ["cliente", "total"],
        "preview_rows": [{"cliente": "Juan", "total": "1500"}],
        "mapping_contexts": [{"context_id": "c1", "entity_type": None}],
    }
    db = AsyncMock()
    with (
        unittest.mock.patch.object(
            llm_file_type_detector,
            "detect_file_type",
            new=AsyncMock(return_value=("ventas", 0.9, "model-x")),
        ),
        unittest.mock.patch.object(
            llm_file_type_detector.pipeline_event_service,
            "emit_event",
            new=AsyncMock(),
        ) as emit,
    ):
        await maybe_detect_file_type(
            db, summary, trace_id=uuid.uuid4(), tenant_id=uuid.uuid4(), file_id=uuid.uuid4()
        )

    assert summary["inferred_type"] == "ventas"
    assert summary["mapping_contexts"][0]["entity_type"] == "sale"
    emit.assert_awaited_once()
    _, kwargs = emit.call_args
    assert kwargs["stage"] == "mapping"
    assert kwargs["detail"]["type"] == "file_type_detection"
    assert kwargs["detail"]["previous_type"] == "general"
    assert kwargs["detail"]["detected_type"] == "ventas"


@pytest.mark.asyncio
async def test_maybe_detect_no_result_keeps_general_and_no_emit() -> None:
    summary: dict[str, Any] = {
        "inferred_type": "general",
        "headers": ["x"],
        "preview_rows": [{"x": "1"}],
    }
    db = AsyncMock()
    with (
        unittest.mock.patch.object(
            llm_file_type_detector,
            "detect_file_type",
            new=AsyncMock(return_value=(None, 0.0, "model-x")),
        ),
        unittest.mock.patch.object(
            llm_file_type_detector.pipeline_event_service,
            "emit_event",
            new=AsyncMock(),
        ) as emit,
    ):
        await maybe_detect_file_type(
            db, summary, trace_id=uuid.uuid4(), tenant_id=uuid.uuid4(), file_id=uuid.uuid4()
        )

    assert summary["inferred_type"] == "general"  # intacto
    emit.assert_not_awaited()
