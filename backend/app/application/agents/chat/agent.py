"""AgentChat — el agente que habla directamente con el usuario.

Responsabilidades:
- Recibe el resultado de los sub-agentes (AgentResponse) + contextos de negocio
- Genera la respuesta en lenguaje natural con Sonnet (español rioplatense)
- Detecta ambigüedad → genera UNA pregunta de aclaración concreta
- Presenta pending actions para aprobación con resumen claro (no JSON crudo)
- Sintetiza resultados multi-agente en una respuesta coherente

Este agente NUNCA accede a la base de datos directamente.
Toda la información le llega via AgentResponse y los contextos inyectados por ChatOrchestrator.

Modelo: claude-sonnet-4-6 (max_tokens=1200)
"""

from __future__ import annotations

import re
import uuid
from typing import TYPE_CHECKING, Any

import anthropic

from app.application.agents.base import BaseAgent
from app.application.agents.shared.schemas import (
    AgentRequest,
    AgentResponse,
    LLMCall,
    RiskLevel,
    UsageSummary,
)
from app.application.security.prompt_defense import wrap_user_input
from app.domain.verticals import VERTICAL_LABELS, Vertical
from app.integrations.anthropic_client import get_anthropic_async_client
from app.observability.logger import get_logger

if TYPE_CHECKING:
    from redis.asyncio import Redis
    from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger(__name__)

_MODEL = "claude-sonnet-4-6"
_MAX_TOKENS = 1200


class AgentChat(BaseAgent):
    """Agente de síntesis y comunicación con el usuario.

    Siempre es el último en hablar: toma lo que produjeron los agentes de dominio
    y lo convierte en una respuesta clara y accionable para el usuario.
    """

    agent_name = "agent_chat"

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

    async def process(self, request: AgentRequest, task: Any | None = None) -> AgentResponse:
        """Implementación mínima de BaseAgent — en la práctica se llama generate_response."""
        msg, call = await self.generate_response(
            request=request,
            agent_response=AgentResponse(
                request_id=request.request_id,
                agent_name="none",
                status="success",
                risk_level=RiskLevel.LOW,
                result={},
            ),
            business_name="el negocio",
            # Camino de saludo / sin agente: legítimamente no hay contexto de
            # tenant. `None` explícito — nunca un vertical inventado.
            business_type=None,
            heuristics=None,
            conversation_ctx={},
        )
        return AgentResponse(
            request_id=request.request_id,
            agent_name=self.agent_name,
            status="success",
            risk_level=RiskLevel.LOW,
            message=msg,
            usage=UsageSummary(calls=[call]),
        )

    async def generate_response(
        self,
        request: AgentRequest,
        agent_response: AgentResponse,
        business_name: str,
        business_type: Vertical | None,
        heuristics: object | None,
        conversation_ctx: dict[str, Any],
        bm_data: dict[str, Any] | None = None,
        agent_memory_fragment: str = "",
        file_context: str = "",
        tenant_id: uuid.UUID | None = None,
        db: AsyncSession | None = None,
        redis: Redis | None = None,
        degraded_notice: str | None = None,
    ) -> tuple[str, LLMCall]:
        """Genera la respuesta final al usuario en lenguaje natural.

        Retorna (texto_respuesta, LLMCall).

        Args:
            degraded_notice: Cuando el contexto de negocio no se pudo cargar, el
                orchestrator pasa una nota que se antepone al system prompt para que el
                LLM sea prudente y no afirme datos que no tiene.
        """
        system = await self._build_system_prompt(
            request=request,
            agent_response=agent_response,
            business_name=business_name,
            business_type=business_type,
            heuristics=heuristics,
            conversation_ctx=conversation_ctx,
            agent_memory_fragment=agent_memory_fragment,
            file_context=file_context,
            tenant_id=tenant_id,
            db=db,
            redis=redis,
        )
        if degraded_notice is not None:
            system = f"{degraded_notice}\n\n{system}"

        user_content = (
            f"Mensaje del usuario: {wrap_user_input(request.message)}\n\n"
            f"Resultado del análisis:\n{self._format_agent_result(agent_response)}"
        )

        response = await self.client.messages.create(
            model=_MODEL,
            max_tokens=_MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user_content}],
        )

        chat_call = LLMCall(
            source="agent_chat",
            model=_MODEL,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        raw = response.content[0].text.strip() if response.content else "Procesado."
        return self._strip_markdown(raw), chat_call

    # ── Construcción del system prompt ────────────────────────────────────────

    async def _build_system_prompt(
        self,
        request: AgentRequest,
        agent_response: AgentResponse,
        business_name: str,
        business_type: Vertical | None,
        heuristics: object | None,
        conversation_ctx: dict[str, Any],
        agent_memory_fragment: str,
        file_context: str,
        tenant_id: uuid.UUID | None,
        db: AsyncSession | None,
        redis: Redis | None,
    ) -> str:
        from app.application.agents.shared.heuristic_engine import HeuristicConfig  # noqa: PLC0415

        turns = conversation_ctx.get("turns", [])
        history = (
            "\n".join(self._format_history_turn(t) for t in turns[-4:]) or "Sin historial previo."
        )

        heuristic_fragment = (
            heuristics.to_prompt_fragment()
            if isinstance(heuristics, HeuristicConfig)
            else ""
        )

        # Sin vertical (saludo / camino sin agente) el prompt no nombra ningún
        # rubro: mejor omitirlo que atribuirle al negocio uno que no es.
        rubro = f" ({VERTICAL_LABELS[business_type]})" if business_type is not None else ""

        # Datos financieros determinísticos del tenant
        memory_fragment = await self._load_financial_context(tenant_id, db)

        # Historial de sesión (qué se cargó en sesiones previas)
        memory_fragment += await self._load_session_memory(tenant_id, db, redis)

        # Fragmentos opcionales
        agent_mem_section = f"\n\n{agent_memory_fragment}" if agent_memory_fragment else ""
        file_section = (
            f"\n\nArchivos de referencia cargados por el usuario:\n{file_context}"
            if file_context
            else ""
        )

        action_type = agent_response.result.get("action_type", "") if agent_response.result else ""
        action_ctx = f"Acción determinada: {action_type}\n\n" if action_type else ""

        # Regla sobre el estado de la acción
        # Nota: auto_executed se setea en la capa HTTP después de que AgentChat genera la respuesta.
        # Por eso siempre usamos la regla de "pendiente de confirmación" para acciones mutantes.
        # Las acciones de análisis (status=success, sin pending_action_id) no necesitan la regla.
        is_analysis = agent_response.result.get("analysis") if agent_response.result else False
        no_action_rule = ""
        if not action_type or is_analysis:
            no_action_rule = (
                "REGLA CRÍTICA: No se ejecutó ninguna operación en esta respuesta. "
                "NO afirmes que registraste, eliminaste ni modificaste ningún dato. "
                "Respondé solo con información o instrucciones.\n\n"
            )
        elif agent_response.requires_approval:
            no_action_rule = (
                "REGLA CRÍTICA: Esta operación AÚN NO se ejecutó — el usuario debe confirmarla "
                "primero. NO digas que ya fue registrada ni realizada.\n\n"
            )

        return (
            f"Sos el asistente financiero de Véktor para {business_name}{rubro}.\n\n"
            "CAPACIDADES DE VÉKTOR: registro de ventas, gastos, compras y movimientos de caja; "
            "modificar productos (precio, costo, umbral de stock, activar/desactivar, renombrar); "
            "ajustar stock e inventario; preparar borradores de email a proveedores (Gmail); "
            "clasificar mensajes recibidos de proveedores; "
            "crear y consultar eventos en Google Calendar; sincronizar con Google Sheets "
            "y Docs.\n\n"
            "FUENTE DE VERDAD — REGLA CRÍTICA: Véktor es el único lugar donde se modifican los "
            "datos del negocio. NUNCA sugerirle al usuario que vaya a Google Sheets ni ningún "
            "servicio externo a modificar datos. Google se usa solo para importar o exportar "
            "copias opcionales.\n\n"
            f"{no_action_rule}"
            f"{action_ctx}"
            f"{heuristic_fragment}"
            f"{memory_fragment}"
            f"{agent_mem_section}"
            f"{file_section}\n\n"
            "MISIÓN: Generá una respuesta conversacional, clara y accionable basada en los "
            "resultados del análisis.\n\n"
            "REGLAS:\n"
            "- NUNCA respondas 'Listo.' ni frases genéricas vacías.\n"
            "- Si hay datos numéricos, interpretálos en contexto del negocio.\n"
            "- Si hay alertas o sugerencias, destacalas con claridad.\n"
            "- Si el estado es 'requires_clarification', reformulá la pregunta amigablemente.\n"
            "- Máximo 3 párrafos cortos. Tono directo, español rioplatense.\n"
            "- NO uses formato markdown (sin **, sin __, sin #, sin - listas). Solo texto plano.\n"
            f"\nHistorial reciente:\n{history}"
        )

    async def _load_financial_context(
        self, tenant_id: uuid.UUID | None, db: AsyncSession | None
    ) -> str:
        if tenant_id is None or db is None:
            return (
                "\n\nDatos del negocio: SIN_DATOS. "
                "INSTRUCCIÓN CRÍTICA: NO INVENTES NÚMEROS. "
                "Indicá al usuario que debe cargar sus datos de ventas y gastos."
            )
        try:
            from app.application.services.deterministic_finance import (  # noqa: PLC0415
                get_financial_summary,
            )

            fin_data = await get_financial_summary(tenant_id, db)
            if fin_data.get("estado") == "SIN_DATOS":
                return (
                    "\n\nDatos del negocio: SIN_DATOS. "
                    "INSTRUCCIÓN CRÍTICA: NO INVENTES NÚMEROS. "
                    "Indicá al usuario que debe cargar sus datos de ventas y gastos."
                )

            flujo = fin_data.get("flujo_neto_30d", {})
            margen = fin_data.get("margen", {})
            ticket = fin_data.get("ticket_promedio", {})
            lines = ["\n\nDatos financieros del negocio (últimos 30 días):"]
            if flujo:
                lines.append(
                    f"  Ventas: ${flujo.get('total_ventas', 0)} | "
                    f"Gastos: ${flujo.get('total_gastos', 0)} | "
                    f"Flujo neto: ${flujo.get('flujo_neto', 0)}"
                )
            if not margen.get("sin_datos"):
                lines.append(f"  Margen bruto: {margen.get('margen_pct', 0)}%")
            if not ticket.get("sin_datos"):
                lines.append(
                    f"  Ticket promedio: ${ticket.get('ticket_promedio', 0)} "
                    f"({ticket.get('n_transacciones', 0)} transacciones)"
                )
            return "\n".join(lines)
        except Exception as exc:
            logger.warning("agent_chat.financial_context_failed", error=str(exc))
            return (
                "\n\nDatos del negocio: SIN_DATOS (error temporal). "
                "INSTRUCCIÓN CRÍTICA: NO INVENTES NÚMEROS."
            )

    async def _load_session_memory(
        self,
        tenant_id: uuid.UUID | None,
        db: AsyncSession | None,
        redis: Any | None,
    ) -> str:
        if tenant_id is None or db is None or redis is None:
            return ""
        try:
            from app.application.services.chat_memory_service import (  # noqa: PLC0415
                ChatMemoryService,
            )

            session_ctx = await ChatMemoryService().get_session_context_cached(db, redis, tenant_id)
            return "\n\n" + self._render_session_memory(session_ctx)
        except Exception as exc:
            logger.warning("agent_chat.session_memory_failed", error=str(exc))
            return (
                "\n\nHISTORIAL DE CARGAS: No disponible temporalmente. "
                "No inventes información histórica."
            )

    # ── Formateo y helpers ────────────────────────────────────────────────────

    @staticmethod
    def _format_history_turn(turn: dict[str, Any]) -> str:
        role = turn.get("role", "user")
        content = turn.get("content", "")
        prefix = "Usuario" if role == "user" else "Asistente"
        return f"{prefix}: {content}"

    @staticmethod
    def _render_session_memory(ctx: Any) -> str:
        """Formatea el contexto de sesión devuelto por ChatMemoryService."""
        if not ctx:
            return ""
        if isinstance(ctx, str):
            return ctx
        if not isinstance(ctx, dict):
            return str(ctx)
        if not ctx.get("historial_disponible"):
            return (
                "HISTORIAL DE CARGAS: El usuario no tiene cargas previas registradas. "
                "No inventes información histórica."
            )
        ultima = ctx.get("ultima_carga") or {}
        descripcion = ultima.get("descripcion") or "carga registrada"
        registros = ultima.get("registros", 0)
        fecha = str(ultima.get("fecha") or "")[:10] or "fecha desconocida"
        total = ctx.get("total_cargas_registradas", 0)
        return (
            "HISTORIAL DE CARGAS:\n"
            f"Última carga: {descripcion} ({registros} registros, {fecha})\n"
            f"Total cargas registradas: {total}\n"
            "No repitas preguntas sobre datos ya cargados."
        )

    @staticmethod
    def _format_agent_result(agent_response: AgentResponse) -> str:
        result = agent_response.result or {}
        lines: list[str] = []

        if s := result.get("summary"):
            lines.append(f"Resumen: {s}")
        if agent_response.message:
            lines.append(f"Mensaje del agente: {agent_response.message}")
        if s := result.get("health_score"):
            lines.append(f"Score de salud: {s}/100")
        if (comps := result.get("components")) and isinstance(comps, dict):
            for k, v in comps.items():
                lines.append(
                    f"  {k}: {v:.0f}/100" if isinstance(v, float) else f"  {k}: {v}"
                )
        if (alerts := result.get("alerts")) and isinstance(alerts, list):
            for a in alerts[:3]:
                if isinstance(a, dict):
                    lines.append(f"⚠ {a.get('message', str(a))}")
                else:
                    lines.append(f"⚠ {a}")
        if q := agent_response.question:
            lines.append(f"Pregunta pendiente: {q}")
        if agent_response.status == "error":
            lines.append("Estado: ERROR")
        elif agent_response.status == "requires_clarification":
            lines.append("Estado: requiere aclaración del usuario")
        elif agent_response.status == "requires_approval":
            lines.append("Estado: pendiente de aprobación del usuario")
        elif agent_response.status == "requires_google_auth":
            lines.append("Estado: requiere autorización de Google")

        # Datos estructurados de la operación (truncados para no sobrecargar el prompt)
        structured = (
            result.get("structured_data") or result.get("entities") or result.get("payload")
        )
        if isinstance(structured, dict):
            relevant = {
                k: v
                for k, v in structured.items()
                if k not in ("conversation_id", "mode", "email_mode") and v is not None
            }
            if relevant:
                lines.append(f"Datos de la operación: {str(relevant)[:300]}")

        return "\n".join(lines) or str(result)[:300]

    @staticmethod
    def _strip_markdown(text: str) -> str:
        text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.DOTALL)
        text = re.sub(r"\*(.+?)\*", r"\1", text, flags=re.DOTALL)
        text = re.sub(r"__(.+?)__", r"\1", text, flags=re.DOTALL)
        text = re.sub(r"_(.+?)_", r"\1", text, flags=re.DOTALL)
        text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
        return text
