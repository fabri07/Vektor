"""AgentHelper — soporte y documentación de Véktor.

Responde preguntas sobre cómo usar la plataforma usando el manual YAML.
Si detecta una pregunta sobre datos de negocio (ventas, gastos, etc.) →
retorna `result["redirect_to"] = "main_chat"` para que el frontend muestre
el banner de redirección al chat principal.

Context Budget: 2.500 tokens.
"""

from __future__ import annotations

import json
from typing import Any

import anthropic

from app.application.agents.base import BaseAgent
from app.application.agents.shared.schemas import (
    ActionType,
    AgentRequest,
    AgentResponse,
    Confidence,
    LLMCall,
    RiskLevel,
    UsageSummary,
)
from app.application.security.prompt_defense import wrap_user_input
from app.application.services.help_documentation_service import (
    format_faq_context,
    is_business_data_question,
    search,
)
from app.integrations.anthropic_client import get_anthropic_async_client
from app.observability.logger import get_logger

logger = get_logger(__name__)

FALLBACK_RESPONSE = (
    "Todavía no tengo información específica sobre eso en mi base de conocimiento. "
    "Podés escribirnos a soporte@vek7or.com o revisar el manual en el panel de Ayuda."
)

_BUSINESS_REDIRECT_MSG = (
    "Esa pregunta es sobre las operaciones de tu negocio. "
    "El chat principal es el lugar indicado: podés decir "
    "'vendí X pesos', 'pagué X de alquiler', '¿cómo está el negocio?', etc."
)

_SYSTEM_TEMPLATE = """\
Sos el asistente de soporte de Véktor. Respondé SOLO preguntas sobre cómo
usar la plataforma Véktor. Si la pregunta es sobre operaciones del negocio
(cargar ventas, gastos, consultar stock, etc.), indicalo.

{doc_context}

REGLAS ESTRICTAS:
1. Respondé SOLO usando la información de la documentación provista.
2. Si no encontrás la respuesta en la documentación → confidence="LOW", answer=null.
3. NO inventés funcionalidades no documentadas.
4. Si la pregunta es sobre operaciones del negocio → is_platform_question=false.

Retorná SOLO un JSON válido:
{{
  "answer": "<respuesta concreta o null>",
  "confidence": "<HIGH|MEDIUM|LOW>",
  "related_module": "<slug del módulo o null>",
  "is_platform_question": <true|false>
}}
"""


class AgentHelper(BaseAgent):
    agent_name = "agent_helper"

    def __init__(self) -> None:
        self._client: Any | None = None

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = get_anthropic_async_client(anthropic.AsyncAnthropic)
        return self._client

    @client.setter
    def client(self, value: Any) -> None:
        self._client = value

    async def find_answer(self, question: str) -> tuple[dict[str, Any], LLMCall]:
        """Busca respuesta usando el manual YAML + LLM."""
        matches = search(question, max_results=3)
        doc_context = (
            format_faq_context(matches) if matches else "Sin documentación relevante encontrada."
        )

        system = _SYSTEM_TEMPLATE.format(doc_context=doc_context)
        response = await self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=800,
            system=system,
            messages=[{"role": "user", "content": wrap_user_input(question)}],
        )
        llm_call = LLMCall(
            source=self.agent_name,
            model="claude-sonnet-4-6",
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        raw = response.content[0].text.strip() if response.content else ""
        try:
            return json.loads(raw), llm_call
        except (json.JSONDecodeError, ValueError):
            return {
                "answer": None,
                "confidence": "LOW",
                "related_module": None,
                "is_platform_question": False,
            }, llm_call

    async def process(self, request: AgentRequest, task: Any | None = None) -> AgentResponse:
        # Heurística rápida antes de llamar al LLM: detectar pregunta de negocio
        if is_business_data_question(request.message):
            logger.info(
                "agent_helper.business_redirect",
                message_preview=request.message[:60],
            )
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="success",
                risk_level=RiskLevel.LOW,
                result={
                    "summary": _BUSINESS_REDIRECT_MSG,
                    "redirect_to": "main_chat",
                },
            )

        result, helper_call = await self.find_answer(request.message)
        usage = UsageSummary(calls=[helper_call])

        logger.info(
            "agent_helper.answered",
            confidence=result.get("confidence"),
            is_platform_question=result.get("is_platform_question"),
            module=result.get("related_module"),
        )

        # El LLM también puede detectar pregunta de negocio
        if not result.get("is_platform_question"):
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="success",
                risk_level=RiskLevel.LOW,
                result={
                    "summary": _BUSINESS_REDIRECT_MSG,
                    "redirect_to": "main_chat",
                },
                usage=usage,
            )

        # confidence LOW o sin respuesta → fallback
        if result.get("confidence") == "LOW" or not result.get("answer"):
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="success",
                risk_level=RiskLevel.LOW,
                confidence=Confidence.LOW,
                result={"summary": FALLBACK_RESPONSE},
                usage=usage,
            )

        answer = result["answer"]
        if result.get("related_module"):
            answer += f"\n\nMódulo: {result['related_module']}"

        return AgentResponse(
            request_id=request.request_id,
            agent_name=self.agent_name,
            status="success",
            risk_level=RiskLevel.LOW,
            confidence=Confidence.HIGH if result["confidence"] == "HIGH" else Confidence.MEDIUM,
            result={
                "summary": answer,
                "action_type": ActionType.ANSWER_HELP_REQUEST,
                "related_module": result.get("related_module"),
            },
            usage=usage,
        )
