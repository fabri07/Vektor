"""AgentCEO — coordinador del sistema multiagente de Véktor.

Responsabilidades:
- Clasificar el intent del mensaje del usuario (LLM Haiku, rápido y barato)
- Construir un AgentTeamPlan (Stage 1: single-task; Stage 3: multi-task + DAG)
- Mapear intent → action_type → riesgo (determinístico, sin LLM)
- Sintetizar respuestas multi-agente (Stage 3, con Sonnet)

RESTRICCIÓN: AgentCEO NUNCA accede directamente a:
  db.sales, db.inventory, db.cash_movements, db.purchase_orders
"""

import sys
from typing import Any

import anthropic

from app.application.agents.base import BaseAgent
from app.application.agents.ceo.team_plan_builder import (
    _ANALYTIC_FAMILIES,
    INTENT_CATALOG,
    INTENT_TO_ACTION_TYPE,
    INTENT_TO_AGENT,
    build_plan,
)
from app.application.agents.shared.json_parse import parse_llm_json
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
from app.observability.logger import get_logger

logger = get_logger(__name__)

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

    async def classify_intent(
        self,
        message: str,
        has_attachment: bool = False,
        attachment_type: str | None = None,
    ) -> tuple[dict[str, Any], LLMCall]:
        """Clasifica el intent usando NLP + Sonnet. Retorna ({intent, entities}, LLMCall).

        Pre-procesa el mensaje con el normalizer argentino antes de enviarlo al LLM.
        """
        from app.application.agents.shared.nlp_preprocessor import preprocess  # noqa: PLC0415

        prep = preprocess(message)
        normalized = prep.normalized_message

        # Construir anotación NLP con entidades pre-extraídas (hint para el LLM)
        _nlp_hints: list[str] = []
        if amounts := prep.entities.get("amount_hints"):
            _nlp_hints.append(f"montos detectados: {', '.join(str(a) for a in amounts[:3])}")
        if qtys := prep.entities.get("quantity_hints"):
            _nlp_hints.append(f"cantidades: {', '.join(str(q) for q in qtys[:2])}")
        if lemmas := prep.entities.get("action_lemmas"):
            _nlp_hints.append(f"verbos: {', '.join(lemmas[:2])}")
        if pcts := prep.entities.get("percentage_hints"):
            _nlp_hints.append(f"porcentajes: {', '.join(str(p) + '%' for p in pcts[:2])}")
        nlp_annotation = (
            f"[Pre-análisis NLP: {'; '.join(_nlp_hints)}]\n" if _nlp_hints else ""
        )

        # Construir guía de intents dinámicamente desde el catálogo
        _guide_lines: list[str] = []
        for _intent, _info in INTENT_CATALOG.items():
            _triggers = _info.get("triggers", [])
            if isinstance(_triggers, list) and _triggers:
                _examples = ", ".join(f"'{t}'" for t in _triggers[:6])
                _guide_lines.append(f"  {_intent}: {_examples}")
            elif _intent not in ("intent_desconocido", "pedir_aclaracion_sobre_archivo",
                                  "pedir_aclaracion_negocio"):
                _guide_lines.append(f"  {_intent}: {_info.get('desc', '')}")
        _intent_guide = "\n".join(_guide_lines)

        # Guía de entidad `analysis_type`: para los intents analíticos consolidados,
        # el sub-análisis va en la entidad (NO en el intent). Es best-effort —
        # si el usuario no es específico, omitir `analysis_type` y el handler corre
        # el análisis general por defecto.
        _entity_lines = [
            f"  {_fam}: {', '.join(_info['types'].keys())}"
            for _fam, _info in _ANALYTIC_FAMILIES.items()
        ]
        _entity_guide = "\n".join(_entity_lines)
        attachment_type_label = attachment_type or "desconocido"
        attachment_context = ""
        if has_attachment:
            attachment_context = (
                "\n=== CONTEXTO DE ARCHIVO ADJUNTO ===\n"
                f"Hay un archivo adjunto de tipo {attachment_type_label}. "
                "Si el usuario pide importar, cargar o subir los datos del archivo (o dice "
                "'anotá/registrá los registros del archivo', 'cargá esto', 'metelos donde "
                "corresponda'), elegí importar_archivo_ventas, importar_archivo_gastos o "
                "importar_archivo_productos según el tipo del archivo. "
                "Mapeo: product/stock → importar_archivo_productos; expense → "
                "importar_archivo_gastos; sale/mixed/desconocido → importar_archivo_ventas. "
                "Si el usuario pide analizar, revisar, mirar, comparar, resumir o diagnosticar "
                "el archivo, elegí analizar_archivo, analizar_precios o analizar_stock según "
                "corresponda.\n"
                "EXCEPCIÓN IMPORTANTE: si el usuario menciona el monto o la cantidad de UNA "
                "operación puntual (ej. 'anotame una venta de $1200', 'registrá un gasto de "
                "$500 de luz'), eso es un REGISTRO MANUAL — clasificá ingresar_venta o "
                "registrar_gasto, NO una importación, aunque haya un adjunto presente.\n"
                "El CEO no toca la base de datos: solo clasifica.\n"
            )

        system = (
            "Sos el clasificador de intenciones de Véktor, sistema de gestión financiera para "
            "negocios argentinos (kioscos, almacenes, distribuidoras, locales de limpieza y "
            "decoración).\n"
            "Hablás con dueños de pequeños negocios en Argentina — español rioplatense, voseo, "
            "lunfardo de negocio. Tu tarea: entender QUÉ quieren hacer y retornar el intent "
            "correcto.\n\n"
            f"Intenciones válidas: {', '.join(INTENT_CATALOG)}\n\n"
            "=== DISPARADORES POR INTENT (cómo hablan realmente los usuarios) ===\n\n"
            f"{_intent_guide}\n\n"
            "=== ENTIDAD analysis_type (sub-análisis dentro de un intent analítico) ===\n"
            "Para estos intents, si el usuario es ESPECÍFICO sobre el sub-análisis, agregá "
            "`analysis_type` en entities con uno de los valores listados. Si es genérico, "
            "OMITILO (corre el análisis general). Valores válidos por intent:\n"
            f"{_entity_guide}\n"
            f"{attachment_context}\n"
            "REGLA CRÍTICA — intent_desconocido:\n"
            "Usá 'intent_desconocido' si el mensaje NO está relacionado con:\n"
            "  - Operaciones del negocio: ventas, gastos, compras, caja, stock, proveedores\n"
            "  - Salud financiera o análisis del negocio\n"
            "  - Uso de la plataforma Véktor\n"
            "  - Eventos de Google Calendar o emails de proveedores\n"
            "Ejemplos de out-of-scope: programación, código, historia, ciencias, recetas, "
            "medicina, entretenimiento, política.\n\n"
            "Retorná SOLO un JSON con:\n"
            '{"intent": "<una de las intenciones válidas>", '
            '"entities": {...campos relevantes: monto, producto, proveedor, fecha, '
            'porcentaje, analysis_type, etc.}}\n\n'
            "Si no podés clasificar → "
            '{"intent": "intent_desconocido", "entities": {}}\n'
            "NO retornes nada más que el JSON. Sin texto adicional."
        )
        response = await self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=800,
            system=system,
            messages=[
                {"role": "user", "content": f"{nlp_annotation}{wrap_user_input(normalized)}"}
            ],
        )
        llm_call = LLMCall(
            source="ceo",
            model="claude-sonnet-4-6",
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        text = (response.content[0].text if response.content else "").strip()
        parsed = parse_llm_json(text)
        if parsed is None:
            return {"intent": "intent_desconocido", "entities": {}}, llm_call

        if parsed.get("intent") not in INTENT_CATALOG:
            parsed["intent"] = "intent_desconocido"
        return parsed, llm_call

    def build_team_plan(self, intent: str, entities: dict[str, Any]) -> AgentTeamPlan:
        """Construye el AgentTeamPlan para el intent clasificado.

        Stage 1: siempre single-task.
        Stage 3 extenderá esto con planes multi-task y DAGs.
        """
        return build_plan(intent, entities)

    async def process(self, request: AgentRequest, task: Any | None = None) -> AgentResponse:
        # 1. Clasificar intent vía LLM Haiku
        attachment_meta = request.context.get("attachment_meta", {})
        classified, ceo_call = await self.classify_intent(
            request.message,
            has_attachment=bool(attachment_meta.get("has_attachment")),
            attachment_type=attachment_meta.get("attachment_type"),
        )
        intent: str = classified.get("intent", "intent_desconocido")
        entities: dict[str, Any] = classified.get("entities", {})
        logger.info(
            "agent_ceo.intent_classified",
            agent_name=self.agent_name,
            intent=intent,
            entity_keys=list(entities.keys()),
        )

        # 2. Construir AgentTeamPlan (Stage 1: single-task)
        plan = self.build_team_plan(intent, entities)

        # 3. Extraer campos del primer task para backward compat
        first_task = plan.tasks[0] if plan.tasks else None
        action_type: ActionType = (
            first_task.action_type if first_task else ActionType.ANSWER_HELP_REQUEST
        )
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
                "target_agent": target_agent,  # legacy — orchestrator lo lee si no hay plan
                "intent": intent,
                "entities": entities,
                "action_type": str(action_type),
                "plan": plan.model_dump(),  # Stage 1: plan single-task
            },
            usage=UsageSummary(calls=[ceo_call]),
        )
