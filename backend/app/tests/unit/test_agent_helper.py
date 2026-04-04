"""Unit tests for AgentHelper — no real LLM calls, no DB."""

import json
import unittest.mock
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.application.agents.shared.schemas import AgentRequest, Confidence, RiskLevel
from app.application.agents.helper.agent import FALLBACK_RESPONSE


def _make_request(message: str = "test") -> AgentRequest:
    return AgentRequest(
        user_id="user-123",
        business_id="tenant-456",
        message=message,
    )


def _mock_llm_response(payload: dict) -> MagicMock:
    content_block = MagicMock()
    content_block.text = json.dumps(payload)
    response = MagicMock()
    response.content = [content_block]
    return response


@pytest.mark.asyncio
async def test_known_question_how_to_load_sale():
    """'¿cómo cargo una venta?' → respuesta con pasos, status=success, confidence HIGH/MEDIUM."""
    mock_payload = {
        "answer": "Escribí en el chat 'vendí X pesos'. Te pedirá confirmación antes de guardar.",
        "confidence": "HIGH",
        "related_module": "Chat",
        "is_platform_question": True,
    }
    with unittest.mock.patch(
        "app.application.agents.helper.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=_mock_llm_response(mock_payload))
        mock_cls.return_value = mock_client

        from app.application.agents.helper.agent import AgentHelper

        agent = AgentHelper()
        agent.client = mock_client
        result = await agent.process(_make_request("¿cómo cargo una venta?"))

    assert result.status == "success"
    assert result.risk_level == RiskLevel.LOW
    assert result.confidence in (Confidence.HIGH, Confidence.MEDIUM)
    assert "vendí" in result.result["summary"].lower() or "chat" in result.result["summary"].lower()
    assert result.result.get("action_type") == "ANSWER_HELP_REQUEST"


@pytest.mark.asyncio
async def test_unknown_question_returns_fallback():
    """'¿cuánto vale el dólar?' → FALLBACK_RESPONSE sin inventar."""
    mock_payload = {
        "answer": None,
        "confidence": "LOW",
        "related_module": None,
        "is_platform_question": True,
    }
    with unittest.mock.patch(
        "app.application.agents.helper.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=_mock_llm_response(mock_payload))
        mock_cls.return_value = mock_client

        from app.application.agents.helper.agent import AgentHelper

        agent = AgentHelper()
        agent.client = mock_client
        result = await agent.process(_make_request("¿cuánto vale el dólar?"))

    assert result.status == "success"
    assert result.result["summary"] == FALLBACK_RESPONSE
    assert result.confidence == Confidence.LOW


@pytest.mark.asyncio
async def test_non_platform_question_redirects():
    """'¿cuánto vendí ayer?' → redirige al chat, no responde con datos."""
    mock_payload = {
        "answer": None,
        "confidence": "LOW",
        "related_module": None,
        "is_platform_question": False,
    }
    with unittest.mock.patch(
        "app.application.agents.helper.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=_mock_llm_response(mock_payload))
        mock_cls.return_value = mock_client

        from app.application.agents.helper.agent import AgentHelper

        agent = AgentHelper()
        agent.client = mock_client
        result = await agent.process(_make_request("¿cuánto vendí ayer?"))

    assert result.status == "success"
    assert "chat" in result.result["summary"].lower()
    # No debe retornar FALLBACK_RESPONSE — es una redirección, no un fallback
    assert result.result["summary"] != FALLBACK_RESPONSE


@pytest.mark.asyncio
async def test_never_invents_features():
    """Pregunta sobre función no documentada → fallback o 'no disponible', NUNCA inventar."""
    mock_payload = {
        "answer": None,
        "confidence": "LOW",
        "related_module": None,
        "is_platform_question": True,
    }
    with unittest.mock.patch(
        "app.application.agents.helper.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=_mock_llm_response(mock_payload))
        mock_cls.return_value = mock_client

        from app.application.agents.helper.agent import AgentHelper

        agent = AgentHelper()
        agent.client = mock_client
        result = await agent.process(
            _make_request("¿puedo conectar Véktor con Mercado Libre?")
        )

    assert result.status == "success"
    assert result.result["summary"] == FALLBACK_RESPONSE
    assert result.confidence == Confidence.LOW


@pytest.mark.asyncio
async def test_low_confidence_uses_fallback():
    """LLM retorna confidence=LOW → FALLBACK_RESPONSE, NUNCA la respuesta inventada."""
    mock_payload = {
        "answer": "Quizás podés intentar en configuración...",
        "confidence": "LOW",
        "related_module": None,
        "is_platform_question": True,
    }
    with unittest.mock.patch(
        "app.application.agents.helper.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=_mock_llm_response(mock_payload))
        mock_cls.return_value = mock_client

        from app.application.agents.helper.agent import AgentHelper

        agent = AgentHelper()
        agent.client = mock_client
        result = await agent.process(_make_request("¿cómo configuro algo raro?"))

    assert result.result["summary"] == FALLBACK_RESPONSE
    assert result.confidence == Confidence.LOW


@pytest.mark.asyncio
async def test_answer_does_not_modify_data():
    """process() nunca llama a servicios de escritura — solo LLM."""
    mock_payload = {
        "answer": "Para cargar una venta, escribí 'vendí X pesos' en el chat.",
        "confidence": "HIGH",
        "related_module": "Chat",
        "is_platform_question": True,
    }
    with unittest.mock.patch(
        "app.application.agents.helper.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=_mock_llm_response(mock_payload))
        mock_cls.return_value = mock_client

        # Verificar que no se importan servicios de escritura en el módulo
        with unittest.mock.patch(
            "app.application.agents.helper.agent.json.loads",
            wraps=json.loads,
        ) as mock_json:
            from app.application.agents.helper.agent import AgentHelper

            agent = AgentHelper()
            agent.client = mock_client
            result = await agent.process(_make_request("¿cómo cargo una venta?"))

    # La respuesta debe ser success sin requires_approval (no modifica datos)
    assert result.status == "success"
    assert result.requires_approval is False
    assert result.risk_level == RiskLevel.LOW
    # Solo una llamada al LLM (find_answer), nada más
    assert mock_client.messages.create.call_count == 1


@pytest.mark.asyncio
async def test_related_module_included_when_available():
    """Respuesta sobre inventario → related_module='Inventario' incluido en el summary."""
    mock_payload = {
        "answer": "En el módulo de Inventario podés ver el catálogo y el stock actual.",
        "confidence": "HIGH",
        "related_module": "Inventario",
        "is_platform_question": True,
    }
    with unittest.mock.patch(
        "app.application.agents.helper.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=_mock_llm_response(mock_payload))
        mock_cls.return_value = mock_client

        from app.application.agents.helper.agent import AgentHelper

        agent = AgentHelper()
        agent.client = mock_client
        result = await agent.process(_make_request("¿cómo veo mi inventario?"))

    assert result.status == "success"
    assert result.result.get("related_module") == "Inventario"
    assert "Inventario" in result.result["summary"]
