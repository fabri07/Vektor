"""Tests unitarios para la confianza real del CEO (Task 1 — F0.1).

Verifica:
- _confidence_from_float mapea los 3 tramos correctamente
- classify_intent extrae confidence/reasoning/ambiguous_with con defaults defensivos
- process() incluye confidence_float/reasoning/ambiguous_with en el result y setea el enum
"""

import json
import unittest.mock
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.application.agents.shared.schemas import Confidence, LLMCall, AgentRequest


def _make_request(message: str = "test") -> AgentRequest:
    return AgentRequest(
        user_id="user-123",
        business_id="tenant-456",
        message=message,
    )


def _mock_llm_response_with_confidence(
    intent: str,
    entities: dict[str, Any] | None = None,
    confidence: float | None = None,
    reasoning: str | None = None,
    ambiguous_with: list[str] | None = None,
) -> MagicMock:
    """Construye un mock del response del cliente Anthropic con campos de confianza."""
    payload: dict[str, Any] = {
        "intent": intent,
        "entities": entities or {},
    }
    if confidence is not None:
        payload["confidence"] = confidence
    if reasoning is not None:
        payload["reasoning"] = reasoning
    if ambiguous_with is not None:
        payload["ambiguous_with"] = ambiguous_with

    content_block = MagicMock()
    content_block.text = json.dumps(payload)
    response = MagicMock()
    response.content = [content_block]
    response.usage = MagicMock(input_tokens=50, output_tokens=20)
    return response


# ── Tests de _confidence_from_float ──────────────────────────────────────────

def test_confidence_from_float_high():
    """>= 0.85 → Confidence.HIGH."""
    from app.application.agents.ceo.agent import _confidence_from_float

    assert _confidence_from_float(0.85) == Confidence.HIGH
    assert _confidence_from_float(0.90) == Confidence.HIGH
    assert _confidence_from_float(1.0) == Confidence.HIGH


def test_confidence_from_float_medium():
    """>= 0.72 y < 0.85 → Confidence.MEDIUM."""
    from app.application.agents.ceo.agent import _confidence_from_float

    assert _confidence_from_float(0.72) == Confidence.MEDIUM
    assert _confidence_from_float(0.80) == Confidence.MEDIUM
    assert _confidence_from_float(0.84) == Confidence.MEDIUM


def test_confidence_from_float_low():
    """< 0.72 → Confidence.LOW."""
    from app.application.agents.ceo.agent import _confidence_from_float

    assert _confidence_from_float(0.0) == Confidence.LOW
    assert _confidence_from_float(0.50) == Confidence.LOW
    assert _confidence_from_float(0.71) == Confidence.LOW


# ── Tests de classify_intent — parseo de campos de confianza ─────────────────

@pytest.mark.asyncio
async def test_classify_intent_confidence_present():
    """Cuando el LLM devuelve confidence=0.9, el dict lo incluye correctamente."""
    with unittest.mock.patch(
        "app.application.agents.ceo.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(
            return_value=_mock_llm_response_with_confidence(
                "ingresar_venta",
                confidence=0.9,
                reasoning="El usuario mencionó venta.",
                ambiguous_with=[],
            )
        )
        mock_cls.return_value = mock_client

        from app.application.agents.ceo.agent import AgentCEO

        agent = AgentCEO()
        agent.client = mock_client
        result, llm_call = await agent.classify_intent("vendí 100 pesos")

    assert result["confidence"] == 0.9
    assert result["reasoning"] == "El usuario mencionó venta."
    assert result["ambiguous_with"] == []
    assert isinstance(llm_call, LLMCall)


@pytest.mark.asyncio
async def test_classify_intent_confidence_absent_defaults_to_0_5():
    """Cuando el LLM omite 'confidence', el dict usa 0.5 como default defensivo."""
    with unittest.mock.patch(
        "app.application.agents.ceo.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        # Respuesta sin campo confidence
        mock_client.messages.create = AsyncMock(
            return_value=_mock_llm_response_with_confidence("ingresar_gasto")
        )
        mock_cls.return_value = mock_client

        from app.application.agents.ceo.agent import AgentCEO

        agent = AgentCEO()
        agent.client = mock_client
        result, _ = await agent.classify_intent("gasté en luz")

    assert result["confidence"] == 0.5


@pytest.mark.asyncio
async def test_classify_intent_confidence_out_of_range_defaults_to_0_5():
    """Cuando confidence está fuera de [0,1] (>1), usar 0.5."""
    with unittest.mock.patch(
        "app.application.agents.ceo.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(
            return_value=_mock_llm_response_with_confidence(
                "ingresar_venta",
                confidence=1.5,  # fuera de rango por arriba
            )
        )
        mock_cls.return_value = mock_client

        from app.application.agents.ceo.agent import AgentCEO

        agent = AgentCEO()
        agent.client = mock_client
        result, _ = await agent.classify_intent("vendí algo")

    assert result["confidence"] == 0.5


@pytest.mark.asyncio
async def test_classify_intent_confidence_negative_clamps_to_0_5():
    """Cuando confidence es negativa (e.g. -0.5), debe defaultear a 0.5."""
    with unittest.mock.patch(
        "app.application.agents.ceo.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(
            return_value=_mock_llm_response_with_confidence(
                "ingresar_venta",
                confidence=-0.5,  # fuera de rango por abajo
            )
        )
        mock_cls.return_value = mock_client

        from app.application.agents.ceo.agent import AgentCEO

        agent = AgentCEO()
        agent.client = mock_client
        result, _ = await agent.classify_intent("algo raro")

    assert result["confidence"] == 0.5


@pytest.mark.asyncio
async def test_classify_intent_reasoning_absent_defaults_empty():
    """Cuando reasoning está ausente, default ''."""
    with unittest.mock.patch(
        "app.application.agents.ceo.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(
            return_value=_mock_llm_response_with_confidence("ingresar_venta", confidence=0.8)
        )
        mock_cls.return_value = mock_client

        from app.application.agents.ceo.agent import AgentCEO

        agent = AgentCEO()
        agent.client = mock_client
        result, _ = await agent.classify_intent("vendí algo")

    assert result["reasoning"] == ""


@pytest.mark.asyncio
async def test_classify_intent_ambiguous_with_absent_defaults_empty_list():
    """Cuando ambiguous_with está ausente, default []."""
    with unittest.mock.patch(
        "app.application.agents.ceo.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(
            return_value=_mock_llm_response_with_confidence("ingresar_venta", confidence=0.8)
        )
        mock_cls.return_value = mock_client

        from app.application.agents.ceo.agent import AgentCEO

        agent = AgentCEO()
        agent.client = mock_client
        result, _ = await agent.classify_intent("vendí algo")

    assert result["ambiguous_with"] == []


# ── Tests de process() — campos en result y enum de confianza ─────────────────

@pytest.mark.asyncio
async def test_process_result_includes_confidence_float():
    """process() incluye confidence_float en el result."""
    with unittest.mock.patch(
        "app.application.agents.ceo.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(
            return_value=_mock_llm_response_with_confidence(
                "ingresar_venta",
                confidence=0.9,
                reasoning="Venta detectada.",
                ambiguous_with=[],
            )
        )
        mock_cls.return_value = mock_client

        from app.application.agents.ceo.agent import AgentCEO

        agent = AgentCEO()
        agent.client = mock_client
        response = await agent.process(_make_request("vendí 100 pesos"))

    assert "confidence_float" in response.result
    assert response.result["confidence_float"] == 0.9
    assert "reasoning" in response.result
    assert "ambiguous_with" in response.result


@pytest.mark.asyncio
async def test_process_sets_confidence_high_from_float():
    """confidence >= 0.85 → Confidence.HIGH en el AgentResponse."""
    with unittest.mock.patch(
        "app.application.agents.ceo.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(
            return_value=_mock_llm_response_with_confidence("ingresar_venta", confidence=0.95)
        )
        mock_cls.return_value = mock_client

        from app.application.agents.ceo.agent import AgentCEO

        agent = AgentCEO()
        agent.client = mock_client
        response = await agent.process(_make_request("vendí algo"))

    assert response.confidence == Confidence.HIGH


@pytest.mark.asyncio
async def test_process_sets_confidence_medium_from_float():
    """confidence en [0.72, 0.85) → Confidence.MEDIUM."""
    with unittest.mock.patch(
        "app.application.agents.ceo.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(
            return_value=_mock_llm_response_with_confidence("ingresar_venta", confidence=0.80)
        )
        mock_cls.return_value = mock_client

        from app.application.agents.ceo.agent import AgentCEO

        agent = AgentCEO()
        agent.client = mock_client
        response = await agent.process(_make_request("vendí algo"))

    assert response.confidence == Confidence.MEDIUM


@pytest.mark.asyncio
async def test_process_sets_confidence_low_from_float():
    """confidence < 0.72 → Confidence.LOW."""
    with unittest.mock.patch(
        "app.application.agents.ceo.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(
            return_value=_mock_llm_response_with_confidence("ingresar_venta", confidence=0.5)
        )
        mock_cls.return_value = mock_client

        from app.application.agents.ceo.agent import AgentCEO

        agent = AgentCEO()
        agent.client = mock_client
        response = await agent.process(_make_request("vendí algo"))

    assert response.confidence == Confidence.LOW


@pytest.mark.asyncio
async def test_process_no_longer_hardcodes_high():
    """El CEO ya no hardcodea Confidence.HIGH — usa el float del LLM."""
    with unittest.mock.patch(
        "app.application.agents.ceo.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        # Confidence baja → no debería ser HIGH
        mock_client.messages.create = AsyncMock(
            return_value=_mock_llm_response_with_confidence("ingresar_venta", confidence=0.4)
        )
        mock_cls.return_value = mock_client

        from app.application.agents.ceo.agent import AgentCEO

        agent = AgentCEO()
        agent.client = mock_client
        response = await agent.process(_make_request("algo"))

    # Ya no debe ser HIGH si el float fue bajo
    assert response.confidence != Confidence.HIGH


@pytest.mark.asyncio
async def test_process_max_tokens_is_1000():
    """max_tokens del classify_intent debe ser 1000 (subido de 800)."""
    captured_call: dict[str, Any] = {}

    async def capture_create(**kwargs: Any) -> MagicMock:
        captured_call.update(kwargs)
        return _mock_llm_response_with_confidence("ingresar_venta", confidence=0.9)

    with unittest.mock.patch(
        "app.application.agents.ceo.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = capture_create
        mock_cls.return_value = mock_client

        from app.application.agents.ceo.agent import AgentCEO

        agent = AgentCEO()
        agent.client = mock_client
        await agent.classify_intent("vendí algo")

    assert captured_call.get("max_tokens") == 1000
