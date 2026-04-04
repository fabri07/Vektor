import json

import anthropic

from app.application.agents.base import BaseAgent
from app.application.agents.shared.schemas import (
    ActionType,
    AgentRequest,
    AgentResponse,
    Confidence,
    RiskLevel,
)
from app.application.security.prompt_defense import wrap_user_input

client = anthropic.AsyncAnthropic()

FALLBACK_RESPONSE = (
    "Todavía no tengo información específica sobre eso en mi base de conocimiento. "
    "Podés escribirnos a soporte@vek7or.com o revisar el manual en el panel de Ayuda."
)

FAQ_CONTENT = """
PREGUNTAS FRECUENTES DE VÉKTOR:

P: ¿Cómo cargo una venta?
R: Escribí en el chat "vendí X pesos" o "vendí [cantidad] [producto] a $X". Te pedirá confirmación antes de guardar.

P: ¿Cómo registro un gasto?
R: Escribí "pagué alquiler X pesos" o "gasto de X pesos en [concepto]".

P: ¿Qué es el score de salud?
R: Es un número del 0 al 100. Se calcula en base a caja (35%), inventario (30%), proveedores (15%) y disciplina de carga (20%).

P: ¿Cómo agrego un proveedor?
R: En el panel de Proveedores, hacé clic en "Agregar proveedor".

P: ¿Cómo importo un Excel?
R: Adjuntá el archivo en el chat con el mensaje "importar ventas". Verás un preview antes de guardar.

P: ¿Qué pasa con los correos de proveedores?
R: Véktor puede leer correos a los que les pongas la etiqueta "Véktor" en Gmail.

P: ¿Puedo borrar una venta?
R: Por ahora no desde el chat, pero podés cargar un ajuste negativo.

P: ¿Cómo genero un informe?
R: Escribí "informe de la semana" o "estado del negocio".

MÓDULOS:
- Dashboard: resumen del score y métricas
- Chat: interfaz principal para cargar y consultar
- Ventas: historial de ventas
- Caja: movimientos de dinero
- Inventario: catálogo y stock
- Proveedores: contactos y pedidos
- Informes: reportes por período
"""


class AgentHelper(BaseAgent):
    agent_name = "agent_helper"

    def __init__(self) -> None:
        self.client = client

    async def find_answer(self, question: str) -> dict:
        """
        Busca respuesta en el FAQ y la documentación.
        Si confidence < MEDIUM: retornar fallback, NUNCA inventar.
        """
        system = f"""
Sos el asistente de soporte de Véktor. Respondé SOLO preguntas sobre cómo
usar la plataforma Véktor. Si la pregunta no es sobre la plataforma,
indicalo claramente.

BASE DE CONOCIMIENTO:
{FAQ_CONTENT}

REGLAS ESTRICTAS:
1. Respondé SOLO usando la información de la base de conocimiento.
2. Si no encontrás la respuesta → confidence="LOW", answer=null.
3. NO inventés funcionalidades que no estén documentadas.
4. Si es sobre operaciones del negocio (ventas, stock, etc.) → is_platform_question=false.

Retorná SOLO un JSON:
{{
  "answer": "<respuesta o null si no encontrás>",
  "confidence": "<HIGH|MEDIUM|LOW>",
  "related_module": "<nombre del módulo o null>",
  "is_platform_question": <true|false>
}}
"""
        response = await self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            system=system,
            messages=[{"role": "user", "content": wrap_user_input(question)}],
        )
        return json.loads(response.content[0].text.strip())

    async def process(self, request: AgentRequest) -> AgentResponse:
        result = await self.find_answer(request.message)

        # Si no es pregunta sobre la plataforma
        if not result.get("is_platform_question"):
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="success",
                risk_level=RiskLevel.LOW,
                result={
                    "summary": (
                        "Esta pregunta es sobre las operaciones de tu negocio. "
                        "Podés cargar datos directamente en el chat: "
                        "'vendí X pesos', 'pagué X de alquiler', etc."
                    )
                },
            )

        # Si confidence es LOW: usar fallback, NUNCA inventar
        if result.get("confidence") == "LOW" or not result.get("answer"):
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="success",
                risk_level=RiskLevel.LOW,
                confidence=Confidence.LOW,
                result={"summary": FALLBACK_RESPONSE},
            )

        # Respuesta encontrada
        answer = result["answer"]
        if result.get("related_module"):
            answer += f"\n\n📍 Módulo relacionado: {result['related_module']}"

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
        )
