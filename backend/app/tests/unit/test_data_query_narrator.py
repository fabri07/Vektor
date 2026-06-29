"""Tests unitarios para data_query_narrator — narrador compartido de ANSWER_DATA_QUERY.

Usa un cliente Anthropic mockeado (sin llamadas reales a la API).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.application.agents.shared.data_query_narrator import answer_data_query
from app.application.agents.shared.schemas import LLMCall
from app.application.security.prompt_defense import wrap_user_input

# ── Fakes ─────────────────────────────────────────────────────────────────────


class _FakeUsage:
    input_tokens = 100
    output_tokens = 50


class _FakeContent:
    text = "Tu mejor cliente es Juan García con $45.000 en compras."


class _FakeResponse:
    content = [_FakeContent()]
    usage = _FakeUsage()


def _make_client(response: object | None = None) -> MagicMock:
    """Devuelve un cliente Anthropic mockeado que retorna `response`."""
    if response is None:
        response = _FakeResponse()
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=response)
    return client


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_returns_text_and_llm_call() -> None:
    """answer_data_query devuelve (str, LLMCall) bien formado."""
    client = _make_client()

    text, llm_call = await answer_data_query(
        question="¿Quién fue mi mejor cliente?",
        domain="clientes",
        structured_data={"top_cliente": "Juan García", "monto": 45000},
        business_name="Kiosco El Sol",
        client=client,
    )

    assert text == "Tu mejor cliente es Juan García con $45.000 en compras."
    assert isinstance(llm_call, LLMCall)
    assert llm_call.source == "data_query_narrator"
    assert llm_call.model == "claude-sonnet-4-6"
    assert llm_call.input_tokens == 100
    assert llm_call.output_tokens == 50


@pytest.mark.asyncio
async def test_structured_data_in_prompt() -> None:
    """structured_data aparece serializado en el mensaje enviado al cliente."""
    client = _make_client()

    await answer_data_query(
        question="¿Cuánto debo?",
        domain="caja",
        structured_data={
            "saldo": 12345,
            "deudas": [{"proveedor": "Mayorista Norte", "monto": 5000}],
        },
        business_name="Limpieza Pro",
        client=client,
    )

    call_kwargs = client.messages.create.call_args
    user_message: str = call_kwargs.kwargs["messages"][0]["content"]
    assert "12345" in user_message, "saldo no encontrado en el prompt"
    assert "Mayorista Norte" in user_message, "nombre de proveedor no encontrado en el prompt"


@pytest.mark.asyncio
async def test_question_wrapped_with_user_input() -> None:
    """La pregunta del usuario pasa por wrap_user_input antes del LLM."""
    client = _make_client()
    question = "Cuánto vendí ayer"

    await answer_data_query(
        question=question,
        domain="ventas",
        structured_data={},
        business_name="Test Shop",
        client=client,
    )

    call_kwargs = client.messages.create.call_args
    user_message: str = call_kwargs.kwargs["messages"][0]["content"]
    assert wrap_user_input(question) in user_message, (
        "La pregunta no fue envuelta con wrap_user_input"
    )


@pytest.mark.asyncio
async def test_correct_model_and_max_tokens() -> None:
    """Se llama al modelo correcto con los parámetros esperados."""
    client = _make_client()

    await answer_data_query(
        question="¿Cuánto gasté en sueldos?",
        domain="gastos",
        structured_data={"gastos_sueldos": 80000},
        business_name="Mi Negocio",
        client=client,
    )

    call_kwargs = client.messages.create.call_args
    assert call_kwargs.kwargs["model"] == "claude-sonnet-4-6"
    assert call_kwargs.kwargs["max_tokens"] == 600


@pytest.mark.asyncio
async def test_business_name_in_prompt() -> None:
    """El nombre del negocio aparece en el prompt."""
    client = _make_client()

    await answer_data_query(
        question="¿Qué producto se vende más?",
        domain="stock",
        structured_data={"top_producto": "Coca Cola"},
        business_name="Almacén Don Pedro",
        client=client,
    )

    call_kwargs = client.messages.create.call_args
    user_message: str = call_kwargs.kwargs["messages"][0]["content"]
    assert "Almacén Don Pedro" in user_message


@pytest.mark.asyncio
async def test_domain_in_prompt() -> None:
    """El domain aparece en el prompt enviado al cliente."""
    client = _make_client()

    await answer_data_query(
        question="Resumen de proveedores",
        domain="proveedores",
        structured_data={"total_proveedores": 5},
        business_name="Test",
        client=client,
    )

    call_kwargs = client.messages.create.call_args
    user_message: str = call_kwargs.kwargs["messages"][0]["content"]
    assert "proveedores" in user_message
