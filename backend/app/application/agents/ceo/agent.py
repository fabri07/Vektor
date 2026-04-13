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

import anthropic

from app.application.agents.base import BaseAgent
from app.application.agents.shared.schemas import (
    ActionType,
    AgentRequest,
    AgentResponse,
    Confidence,
    RiskLevel,
)
from app.application.agents.shared.risk_engine import RiskEngine
from app.application.security.prompt_defense import wrap_user_input

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
    "upload_file",
    "ask_business_status",
    "ask_dashboard_report",
    "manage_supplier",
    "ask_platform_help",
    "review_inbox_item",
]

# ── Intent → agente especializado ────────────────────────────────────────────
INTENT_TO_AGENT: dict[str, str] = {
    "record_sale": "agent_cash",
    "record_payment_in": "agent_cash",
    "record_expense": "agent_cash",
    "record_payment_out": "agent_cash",
    "record_stock_loss": "agent_stock",
    "upload_file": "agent_stock",
    "manage_supplier": "agent_supplier",
    "review_inbox_item": "agent_supplier",
    "record_purchase": "agent_supplier",
    "ask_business_status": "agent_health",
    "ask_dashboard_report": "agent_health",
    "ask_platform_help": "agent_helper",
}

# ── Intent → ActionType (catálogo cerrado) ────────────────────────────────────
INTENT_TO_ACTION_TYPE: dict[str, ActionType] = {
    "record_sale": ActionType.REGISTER_SALE,
    "record_payment_in": ActionType.REGISTER_CASH_INFLOW,
    "record_expense": ActionType.REGISTER_EXPENSE,
    "record_purchase": ActionType.REGISTER_PURCHASE,
    "record_payment_out": ActionType.REGISTER_CASH_OUTFLOW,
    "record_stock_loss": ActionType.REGISTER_STOCK_LOSS,
    "upload_file": ActionType.IMPORT_TABULAR_FILE,
    "ask_business_status": ActionType.GENERATE_HEALTH_REPORT,
    "ask_dashboard_report": ActionType.GENERATE_HEALTH_REPORT,
    "manage_supplier": ActionType.CREATE_SUPPLIER_DRAFT,
    "ask_platform_help": ActionType.ANSWER_HELP_REQUEST,
    "review_inbox_item": ActionType.CLASSIFY_GMAIL_MESSAGE,
}


class AgentCEO(BaseAgent):
    agent_name = "agent_ceo"
    client = anthropic.AsyncAnthropic()

    async def classify_intent(self, message: str) -> dict:
        """Clasifica el intent vía LLM Haiku. Retorna {intent, entities}."""
        system = (
            "Sos el clasificador de intenciones de Véktor, un sistema de gestión para PyMEs.\n"
            f"Intenciones válidas: {', '.join(INTENT_CATALOG)}\n\n"
            "Analizá el mensaje del usuario y retorná SOLO un JSON con:\n"
            '{"intent": "<una de las intenciones válidas>", "entities": {...campos relevantes...}}\n\n'
            f"Si no podés clasificar → {{\"intent\": \"ask_platform_help\", \"entities\": {{}}}}\n"
            "NO retornes nada más que el JSON. Sin texto adicional."
        )
        response = await self.client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=300,
            system=system,
            messages=[{"role": "user", "content": wrap_user_input(message)}],
        )
        text = response.content[0].text.strip()
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, IndexError):
            return {"intent": "ask_platform_help", "entities": {}}

        if parsed.get("intent") not in INTENT_CATALOG:
            parsed["intent"] = "ask_platform_help"
        return parsed

    async def process(self, request: AgentRequest) -> AgentResponse:
        # 1. Clasificar intent vía LLM
        classified = await self.classify_intent(request.message)
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
        )
