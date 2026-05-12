"""AgentCEO — router y coordinador del sistema multiagente de Véktor.

Responsabilidades:
- Clasificar el intent del mensaje del usuario (LLM Haiku)
- Mapear intent → action_type → riesgo (determinístico, sin LLM)
- Si riesgo LOW: retornar status=success con target_agent para ejecución directa
- Si riesgo MEDIUM/HIGH: retornar requires_approval=True para crear pending_action

RESTRICCIÓN: AgentCEO NUNCA accede directamente a:
  db.sales, db.inventory, db.cash_movements, db.purchase_orders
"""

import json
import sys
from typing import Any

import anthropic

from app.application.agents.base import BaseAgent
from app.application.agents.shared.risk_engine import RiskEngine
from app.application.agents.shared.schemas import (
    ActionType,
    AgentRequest,
    AgentResponse,
    Confidence,
    LLMCall,
    UsageSummary,
)
from app.application.security.prompt_defense import wrap_user_input
from app.integrations.anthropic_client import get_anthropic_async_client

# ── Guardia de importación ────────────────────────────────────────────────────
_FORBIDDEN = ["db.sales", "db.inventory", "db.cash_movements", "db.purchase_orders"]
for _f in _FORBIDDEN:
    assert _f not in sys.modules, f"AgentCEO no puede importar {_f}"

# ── Catálogo cerrado de intents ───────────────────────────────────────────────
INTENT_CATALOG = [
    "record_sale",
    "record_payment_in",
    "record_expense",
    "record_purchase",
    "record_payment_out",
    "record_stock_loss",
    "update_product",
    "upload_file",
    "ask_business_status",
    "ask_dashboard_report",
    "ask_stock_status",
    "ask_supplier_status",
    "manage_supplier",
    "ask_platform_help",
    "review_inbox_item",
    "schedule_event",
    "check_calendar",
    "sync_google_data",
    "out_of_scope",
]

# ── Intent → agente especializado ────────────────────────────────────────────
INTENT_TO_AGENT: dict[str, str] = {
    "record_sale": "agent_cash",
    "record_payment_in": "agent_cash",
    "record_expense": "agent_cash",
    "record_payment_out": "agent_cash",
    "record_stock_loss": "agent_stock",
    "update_product": "agent_stock",
    "upload_file": "agent_stock",
    "ask_stock_status": "agent_stock",
    "ask_supplier_status": "agent_supplier",
    "manage_supplier": "agent_supplier",
    "review_inbox_item": "agent_supplier",
    "record_purchase": "agent_supplier",
    "ask_business_status": "agent_health",
    "ask_dashboard_report": "agent_health",
    "ask_platform_help": "agent_helper",
    "schedule_event": "agent_calendar",
    "check_calendar": "agent_calendar",
    "sync_google_data": "agent_sync",
}

# ── Intent → ActionType (catálogo cerrado) ────────────────────────────────────
INTENT_TO_ACTION_TYPE: dict[str, ActionType] = {
    "record_sale": ActionType.REGISTER_SALE,
    "record_payment_in": ActionType.REGISTER_CASH_INFLOW,
    "record_expense": ActionType.REGISTER_EXPENSE,
    "record_purchase": ActionType.REGISTER_PURCHASE,
    "record_payment_out": ActionType.REGISTER_CASH_OUTFLOW,
    "record_stock_loss": ActionType.REGISTER_STOCK_LOSS,
    "update_product": ActionType.UPDATE_PRODUCT,
    "upload_file": ActionType.IMPORT_TABULAR_FILE,
    "ask_business_status": ActionType.GENERATE_HEALTH_REPORT,
    "ask_dashboard_report": ActionType.GENERATE_HEALTH_REPORT,
    "ask_stock_status": ActionType.GENERATE_HEALTH_REPORT,
    "ask_supplier_status": ActionType.GENERATE_HEALTH_REPORT,
    "manage_supplier": ActionType.CREATE_SUPPLIER_DRAFT,
    "ask_platform_help": ActionType.ANSWER_HELP_REQUEST,
    "review_inbox_item": ActionType.CLASSIFY_GMAIL_MESSAGE,
    "schedule_event": ActionType.CREATE_CALENDAR_EVENT,
    "check_calendar": ActionType.GENERATE_HEALTH_REPORT,
    "sync_google_data": ActionType.SYNC_TO_GOOGLE,
}


class AgentCEO(BaseAgent):
    agent_name = "agent_ceo"

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

    async def classify_intent(self, message: str) -> tuple[dict, LLMCall]:
        """Clasifica el intent vía LLM Sonnet. Retorna ({intent, entities}, LLMCall)."""
        system = (
            "Sos el clasificador de intenciones de Véktor, un sistema de gestión financiera para PyMEs argentinas.\n"
            f"Intenciones válidas: {', '.join(INTENT_CATALOG)}\n\n"
            "GUÍA DE INTENCIONES — ejemplos en español rioplatense:\n"
            "  ask_stock_status: 'cómo está mi inventario', 'qué productos están bajos', "
            "'me falta stock de algo', 'qué tengo sin stock', 'qué necesito reponer', "
            "'cuántas unidades tengo', 'estado del inventario', 'qué productos faltan'\n"
            "  ask_supplier_status: 'qué proveedores tengo', 'a quién le compré más', "
            "'cuáles son mis proveedores principales', 'últimas compras por proveedor', "
            "'cuánto les compré', 'mis proveedores del mes', 'análisis de proveedores'\n"
            "  ask_business_status: salud financiera general, score del negocio, análisis financiero\n"
            "  record_stock_loss: merma, rotura, pérdida, vencimiento de un producto específico\n"
            "  update_product: cambiar precio de un producto, actualizar costo, modificar umbral "
            "de stock bajo, activar o desactivar un producto, renombrar un producto, cambiar el "
            "estado de un producto (aclarar que el estado es derivado del stock), "
            "ajustar el stock a un valor específico, editar descripción o categoría\n"
            "  manage_supplier: armar email a proveedor, contactar proveedor, hacer pedido\n\n"
            "REGLA CRÍTICA — out_of_scope:\n"
            "Usá 'out_of_scope' si el mensaje NO está relacionado con:\n"
            "  - Operaciones del negocio: ventas, gastos, compras, caja, stock, proveedores\n"
            "  - Salud financiera o análisis del negocio\n"
            "  - Uso de la plataforma Véktor\n"
            "  - Eventos de Google Calendar o emails de proveedores\n"
            "Ejemplos de out_of_scope: programación, código, historia, ciencias, recetas, "
            "medicina, entretenimiento, consultas sin relación con una PyME.\n\n"
            "Analizá el mensaje del usuario y retorná SOLO un JSON con:\n"
            '{"intent": "<una de las intenciones válidas>", "entities": {...campos relevantes...}}\n\n'
            "Si no podés clasificar en ninguna categoría de negocio → "
            "{\"intent\": \"out_of_scope\", \"entities\": {}}\n"
            "NO retornes nada más que el JSON. Sin texto adicional."
        )
        response = await self.client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=300,
            system=system,
            messages=[{"role": "user", "content": wrap_user_input(message)}],
        )
        llm_call = LLMCall(
            source="ceo",
            model="claude-sonnet-4-5",
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        text = (response.content[0].text if response.content else "").strip()
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, IndexError, ValueError):
            return {"intent": "ask_platform_help", "entities": {}}, llm_call

        if parsed.get("intent") not in INTENT_CATALOG:
            parsed["intent"] = "ask_platform_help"
        return parsed, llm_call

    async def process(self, request: AgentRequest) -> AgentResponse:
        # 1. Clasificar intent vía LLM
        classified, ceo_call = await self.classify_intent(request.message)
        intent: str = classified.get("intent", "ask_platform_help")
        entities: dict = classified.get("entities", {})

        # 2. Determinar action_type y agente destino
        action_type = INTENT_TO_ACTION_TYPE.get(intent, ActionType.ANSWER_HELP_REQUEST)
        target_agent = INTENT_TO_AGENT.get(intent, "agent_helper")

        # 3. Evaluar riesgo (determinístico, sin LLM)
        risk_level = RiskEngine.evaluate(action_type)
        requires_approval = RiskEngine.requires_approval(action_type)

        status = "requires_approval" if requires_approval else "success"

        return AgentResponse(
            request_id=request.request_id,
            agent_name=self.agent_name,
            status=status,
            risk_level=risk_level,
            requires_approval=requires_approval,
            confidence=Confidence.HIGH,
            result={
                "target_agent": target_agent,
                "intent": intent,
                "entities": entities,
                "action_type": str(action_type),
            },
            usage=UsageSummary(calls=[ceo_call]),
        )
