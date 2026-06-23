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
Sos el asistente de soporte de Véktor. Tu trabajo es EXPLICAR cómo usar la
plataforma: cómo cargar datos, cómo editar una columna, cómo leer el dashboard,
qué significa cada cosa, dónde está cada función, etc.

IMPORTANTE — distinguí preguntas de operaciones:
- Una pregunta sobre CÓMO/QUÉ/DÓNDE/PARA QUÉ usar una función SIEMPRE es de
  plataforma y la tenés que responder. Ejemplos: "¿cómo cargo una venta?",
  "¿cómo agrego una columna?", "¿cómo veo mis gastos del mes?", "¿qué es el
  score?". Aunque mencionen ventas/gastos/stock, son preguntas de ayuda → SÍ
  las respondés (is_platform_question=true).
- Solo es is_platform_question=false si el usuario REALMENTE quiere ejecutar una
  operación de datos (ej: "vendí 5000", "registrá un gasto de 2000") o si la
  pregunta no tiene nada que ver con usar Véktor.

{doc_context}

REGLAS:
1. Respondé en español rioplatense, claro y concreto, basándote en la
   documentación provista. Podés reformular y combinar la info documentada.
2. NO inventés funcionalidades que no estén en la documentación. Si la
   documentación no cubre el tema, confidence="LOW" y answer=null.
3. Para preguntas de cómo-usar-Véktor, is_platform_question=true.

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
        matches = search(question, max_results=5)
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

        # NOTA: el redirect a chat principal lo decide la heurística determinística
        # de arriba (is_business_data_question), no el LLM. Una pregunta de cómo-usar
        # ("¿cómo cargo una venta?") NO debe redirigir aunque mencione una operación.

        # confidence LOW o sin respuesta → fallback (no redirige)
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
