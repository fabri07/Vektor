"""Tests unitarios para AgentCEO — sin llamadas reales al LLM ni a la DB."""

import json
import unittest.mock
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from app.application.agents.shared.schemas import ActionType, AgentRequest, AgentTeamPlan, LLMCall


def _make_request(message: str = "test") -> AgentRequest:
    return AgentRequest(
        user_id="user-123",
        business_id="tenant-456",
        message=message,
    )


def _mock_llm_response(intent: str, entities: dict[str, Any] | None = None) -> MagicMock:
    """Construye un mock del response del cliente Anthropic."""
    content_block = MagicMock()
    content_block.text = json.dumps({"intent": intent, "entities": entities or {}})
    response = MagicMock()
    response.content = [content_block]
    response.usage = MagicMock(input_tokens=50, output_tokens=20)
    return response


async def test_ceo_classifies_sale_intent():
    """'vendí 100 pesos' debe clasificar como ingresar_venta."""
    with unittest.mock.patch(
        "app.application.agents.ceo.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(
            return_value=_mock_llm_response("ingresar_venta", {"monto": 100})
        )
        mock_cls.return_value = mock_client

        from app.application.agents.ceo.agent import AgentCEO

        agent = AgentCEO()
        agent.client = mock_client

        result = await agent.process(_make_request("vendí 100 pesos"))

    assert result.result["intent"] == "ingresar_venta"
    assert result.result["action_type"] == "REGISTER_SALE"
    assert result.result["target_agent"] == "agent_income"


async def test_ceo_routes_to_correct_agent():
    """ingresar_venta debe enrutar a agent_income."""
    with unittest.mock.patch(
        "app.application.agents.ceo.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=_mock_llm_response("ingresar_venta"))
        mock_cls.return_value = mock_client

        from app.application.agents.ceo.agent import AgentCEO

        agent = AgentCEO()
        agent.client = mock_client
        result = await agent.process(_make_request("vendí algo"))

    assert result.result["target_agent"] == "agent_income"


async def test_ceo_unknown_intent_goes_to_helper():
    """Intent inválido del LLM debe caer en intent_desconocido → agent_helper."""
    with unittest.mock.patch(
        "app.application.agents.ceo.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(
            return_value=_mock_llm_response("intent_que_no_existe")
        )
        mock_cls.return_value = mock_client

        from app.application.agents.ceo.agent import AgentCEO

        agent = AgentCEO()
        agent.client = mock_client
        result = await agent.process(_make_request("bla bla xyz"))

    assert result.result["intent"] == "intent_desconocido"
    assert result.result["target_agent"] == "agent_helper"


async def test_ceo_plan_is_single_task():
    """El CEO retorna un plan con una sola tarea en Stage 1."""
    with unittest.mock.patch(
        "app.application.agents.ceo.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(
            return_value=_mock_llm_response("ingresar_gasto", {"monto": 5000})
        )
        mock_cls.return_value = mock_client

        from app.application.agents.ceo.agent import AgentCEO

        agent = AgentCEO()
        agent.client = mock_client
        result = await agent.process(_make_request("pagué el alquiler $5000"))

    plan_dict = result.result.get("plan")
    assert plan_dict is not None, "El resultado debe incluir un campo 'plan'"
    plan = AgentTeamPlan(**plan_dict)
    assert len(plan.tasks) == 1
    assert plan.tasks[0].action_type == ActionType.REGISTER_EXPENSE
    assert plan.intent == "ingresar_gasto"


async def test_ceo_expense_routes_correctly():
    """ingresar_gasto → agent_expense con ActionType REGISTER_EXPENSE."""
    with unittest.mock.patch(
        "app.application.agents.ceo.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=_mock_llm_response("ingresar_gasto"))
        mock_cls.return_value = mock_client

        from app.application.agents.ceo.agent import AgentCEO

        agent = AgentCEO()
        agent.client = mock_client
        result = await agent.process(_make_request("gasté en servicios"))

    assert result.result["action_type"] == "REGISTER_EXPENSE"
    assert result.result["target_agent"] == "agent_expense"


def test_ceo_forbidden_imports():
    """El módulo AgentCEO no debe importar los módulos de datos prohibidos."""
    import app.application.agents.ceo.agent as ceo_module

    forbidden = ["db.sales", "db.inventory", "db.cash_movements", "db.purchase_orders"]
    module_names = set(dir(ceo_module))
    for f in forbidden:
        assert f not in module_names, f"AgentCEO importó módulo prohibido: {f}"


async def test_prompt_injection_wrapped():
    """El mensaje del usuario debe estar envuelto en <user_message>...</user_message>."""
    captured_call: dict[str, Any] = {}

    async def capture_create(**kwargs):
        captured_call.update(kwargs)
        content_block = MagicMock()
        content_block.text = json.dumps({"intent": "ayuda_plataforma", "entities": {}})
        response = MagicMock()
        response.content = [content_block]
        response.usage = MagicMock(input_tokens=50, output_tokens=20)
        return response

    with unittest.mock.patch(
        "app.application.agents.ceo.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = capture_create
        mock_cls.return_value = mock_client

        from app.application.agents.ceo.agent import AgentCEO

        agent = AgentCEO()
        agent.client = mock_client
        await agent.process(_make_request("mensaje de prueba"))

    messages = captured_call.get("messages", [])
    assert len(messages) == 1
    user_content = messages[0]["content"]
    assert "<user_message>" in user_content
    assert "</user_message>" in user_content


async def test_ceo_uses_sonnet_model():
    """CEO usa claude-sonnet-4-6 para clasificar (upgrade de Haiku a Sonnet)."""
    captured_call: dict[str, Any] = {}

    async def capture_create(**kwargs):
        captured_call.update(kwargs)
        content_block = MagicMock()
        content_block.text = json.dumps({"intent": "consultar_estado_negocio", "entities": {}})
        response = MagicMock()
        response.content = [content_block]
        response.usage = MagicMock(input_tokens=50, output_tokens=20)
        return response

    with unittest.mock.patch(
        "app.application.agents.ceo.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = capture_create
        mock_cls.return_value = mock_client

        from app.application.agents.ceo.agent import AgentCEO

        agent = AgentCEO()
        agent.client = mock_client
        await agent.process(_make_request("cómo está mi negocio"))

    assert captured_call.get("model") == "claude-sonnet-4-6"


async def test_ceo_prompt_includes_attachment_context_for_import():
    captured_call: dict[str, Any] = {}

    async def capture_create(**kwargs):
        captured_call.update(kwargs)
        return _mock_llm_response("importar_archivo_productos")

    with unittest.mock.patch(
        "app.application.agents.ceo.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = capture_create
        mock_cls.return_value = mock_client

        from app.application.agents.ceo.agent import AgentCEO

        agent = AgentCEO()
        agent.client = mock_client
        classified, _ = await agent.classify_intent(
            "importá y anotá los registros",
            has_attachment=True,
            attachment_type="product",
        )

    assert classified["intent"] == "importar_archivo_productos"
    system = captured_call.get("system", "")
    assert "Hay un archivo adjunto de tipo product" in system
    assert "importar_archivo_productos" in system


async def test_ceo_process_passes_attachment_meta_to_classifier():
    from app.application.agents.ceo.agent import AgentCEO

    agent = AgentCEO()
    agent.classify_intent = AsyncMock(
        return_value=(
            {"intent": "importar_archivo_gastos", "entities": {}},
            LLMCall(source="ceo", model="test", input_tokens=1, output_tokens=1),
        )
    )

    request = _make_request("cargá esto")
    request.context["attachment_meta"] = {"has_attachment": True, "attachment_type": "expense"}
    result = await agent.process(request)

    agent.classify_intent.assert_awaited_once_with(
        "cargá esto",
        has_attachment=True,
        attachment_type="expense",
    )
    assert result.result["intent"] == "importar_archivo_gastos"
