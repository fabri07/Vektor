"""AgentCEO — coordinador del sistema multiagente de Véktor.

Responsabilidades:
- Clasificar el intent del mensaje del usuario (LLM Haiku, rápido y barato)
- Construir un AgentTeamPlan (Stage 1: single-task; Stage 3: multi-task + DAG)
- Mapear intent → action_type → riesgo (determinístico, sin LLM)
- Sintetizar respuestas multi-agente (Stage 3, con Sonnet)

RESTRICCIÓN: AgentCEO NUNCA accede directamente a:
  db.sales, db.inventory, db.cash_movements, db.purchase_orders
"""

import json
import sys
from typing import Any

import anthropic

from app.application.agents.base import BaseAgent
from app.application.agents.ceo.team_plan_builder import (
    INTENT_CATALOG,
    INTENT_TO_ACTION_TYPE,
    INTENT_TO_AGENT,
    build_plan,
)
from app.application.agents.shared.risk_engine import RiskEngine
from app.application.agents.shared.schemas import (
    ActionType,
    AgentRequest,
    AgentResponse,
    AgentTeamPlan,
    Confidence,
    LLMCall,
    UsageSummary,
)
from app.application.security.prompt_defense import wrap_user_input
from app.integrations.anthropic_client import get_anthropic_async_client

# Re-export para backward compat (test_base_structure.py importa desde aquí)
__all__ = ["AgentCEO", "INTENT_CATALOG", "INTENT_TO_AGENT", "INTENT_TO_ACTION_TYPE"]

# ── Guardia de importación ────────────────────────────────────────────────────
_FORBIDDEN = ["db.sales", "db.inventory", "db.cash_movements", "db.purchase_orders"]
for _f in _FORBIDDEN:
    assert _f not in sys.modules, f"AgentCEO no puede importar {_f}"


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
        """Clasifica el intent vía LLM Haiku. Retorna ({intent, entities}, LLMCall)."""
        system = (
            "Sos el clasificador de intenciones de Véktor, un sistema de gestión financiera para PyMEs argentinas.\n"
            f"Intenciones válidas: {', '.join(INTENT_CATALOG)}\n\n"
            "GUÍA DE INTENCIONES — ejemplos en español rioplatense:\n"
            "  ingresar_venta: 'vendí X unidades', 'hice una venta de $Y', 'venta de producto Z'\n"
            "  ingresar_cobro: 'cobré la deuda de X', 'me pagaron $Y', 'ingresó plata por cobro'\n"
            "  ingresar_gasto: 'pagué el alquiler', 'gasté en servicios', 'compré útiles de oficina'\n"
            "  ingresar_pago_salida: 'pagué una deuda', 'salida de caja por pago', 'transferí $Y'\n"
            "  actualizar_stock: 'ajustá el stock de X', 'tengo Y unidades de Z', 'actualizá inventario'\n"
            "  registrar_merma: 'se rompió X unidades', 'merma de Y', 'vencieron Z productos'\n"
            "  actualizar_producto: 'cambiá el precio de X', 'actualizá el costo de Y', 'renombrá producto'\n"
            "  importar_archivo_ventas: 'subí un Excel de ventas', 'adjunté CSV de ventas', 'importar ventas'\n"
            "  importar_archivo_gastos: 'subí planilla de gastos', 'adjunté CSV de egresos'\n"
            "  registrar_compra_proveedor: 'compré a Mayorista X', 'pedido a proveedor Y', 'orden de compra'\n"
            "  consultar_estado_negocio: 'cómo está mi negocio', 'score financiero', 'salud del negocio'\n"
            "  generar_informe: 'generá un informe', 'reporte del mes', 'dame un análisis completo'\n"
            "  gestionar_proveedor: 'mandá mail a proveedor', 'armá email a X', 'contactar proveedor'\n"
            "  sincronizar_google: 'sincronizá con Sheets', 'exportá a Drive', 'sync Google'\n"
            "  agendar_evento: 'agendá una reunión', 'crear evento en calendario', 'recordatorio para'\n"
            "  ayuda_plataforma: 'cómo uso Véktor', 'qué puedo hacer', 'ayuda con la plataforma'\n\n"
            "REGLA CRÍTICA — intent_desconocido:\n"
            "Usá 'intent_desconocido' si el mensaje NO está relacionado con:\n"
            "  - Operaciones del negocio: ventas, gastos, compras, caja, stock, proveedores\n"
            "  - Salud financiera o análisis del negocio\n"
            "  - Uso de la plataforma Véktor\n"
            "  - Eventos de Google Calendar o emails de proveedores\n"
            "Ejemplos: programación, código, historia, ciencias, recetas, medicina, entretenimiento.\n\n"
            "Retorná SOLO un JSON con:\n"
            '{"intent": "<una de las intenciones válidas>", "entities": {...campos relevantes...}}\n\n'
            "Si no podés clasificar → "
            '{"intent": "intent_desconocido", "entities": {}}\n'
            "NO retornes nada más que el JSON. Sin texto adicional."
        )
        response = await self.client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=300,
            system=system,
            messages=[{"role": "user", "content": wrap_user_input(message)}],
        )
        llm_call = LLMCall(
            source="ceo",
            model="claude-haiku-4-5",
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        text = (response.content[0].text if response.content else "").strip()
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, IndexError, ValueError):
            return {"intent": "intent_desconocido", "entities": {}}, llm_call

        if parsed.get("intent") not in INTENT_CATALOG:
            parsed["intent"] = "intent_desconocido"
        return parsed, llm_call

    def build_team_plan(self, intent: str, entities: dict) -> AgentTeamPlan:
        """Construye el AgentTeamPlan para el intent clasificado.

        Stage 1: siempre single-task.
        Stage 3 extenderá esto con planes multi-task y DAGs.
        """
        return build_plan(intent, entities)

    async def process(self, request: AgentRequest, task: Any | None = None) -> AgentResponse:
        # 1. Clasificar intent vía LLM Haiku
        classified, ceo_call = await self.classify_intent(request.message)
        intent: str = classified.get("intent", "intent_desconocido")
        entities: dict = classified.get("entities", {})

        # 2. Construir AgentTeamPlan (Stage 1: single-task)
        plan = self.build_team_plan(intent, entities)

        # 3. Extraer campos del primer task para backward compat
        first_task = plan.tasks[0] if plan.tasks else None
        action_type: ActionType = first_task.action_type if first_task else ActionType.ANSWER_HELP_REQUEST
        target_agent: str = first_task.agent if first_task else "agent_helper"

        # 4. Riesgo determinístico
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
                "target_agent": target_agent,       # legacy — orchestrator lo lee si no hay plan
                "intent": intent,
                "entities": entities,
                "action_type": str(action_type),
                "plan": plan.model_dump(),           # Stage 1: plan single-task
            },
            usage=UsageSummary(calls=[ceo_call]),
        )
