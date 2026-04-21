"""ChatOrchestrator — capa conversacional entre el endpoint /agent/chat y los sub-agentes.

Flujo:
  1. Cargar contexto del negocio (nombre, tipo) + heurísticas numéricas
  2. Cargar historial de la conversación (ConversationService)
  3. AgentCEO clasifica el intent → nombre del sub-agente destino
  4. Sub-agente ejecuta la lógica de negocio → AgentResponse
  5a. Si requires_approval → message = summary estructurado (sin LLM adicional)
  5b. Si success / requires_clarification → LLM Haiku genera respuesta conversacional rica
  6. Guardar turno en ConversationService (best-effort)
"""

from __future__ import annotations

import uuid

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agents.ceo.agent import AgentCEO
from app.application.agents.registry import get_sub_agent
from app.application.agents.shared.heuristic_engine import HeuristicEngine
from app.application.agents.shared.schemas import AgentRequest, AgentResponse
from app.application.security.prompt_defense import wrap_user_input
from app.application.services.agent_memory_service import AgentMemoryService
from app.application.services.business_memory_service import BusinessMemoryService
from app.application.services.conversation_service import ConversationService
from app.integrations.anthropic_client import AnthropicConfigurationError, get_anthropic_async_client
from app.observability.logger import get_logger
from app.persistence.models.business import BusinessProfile
from app.persistence.models.tenant import Tenant

logger = get_logger(__name__)


class ChatOrchestrator:
    def __init__(self) -> None:
        self.client = get_anthropic_async_client()

    async def handle(
        self,
        request: AgentRequest,
        db: AsyncSession,
        redis: Redis,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
    ) -> AgentResponse:
        # 1. Contexto del negocio
        business_name, business_type = await self._load_business_context(tenant_id, db)
        heuristics = HeuristicEngine.get(business_type)

        # 1b. BusinessMemory — contexto acumulado del negocio (fail-silencioso)
        bm_data: dict = {}
        try:
            bm_svc = BusinessMemoryService(db=db, redis=redis)
            bm_data = await bm_svc.get(tenant_id)
        except Exception:
            pass

        # 1c. AgentMemory — patrones aprendidos del negocio (fail-silencioso)
        agent_memory_fragment = ""
        try:
            am_svc = AgentMemoryService(db=db, redis=redis)
            agent_memory_fragment = await am_svc.get_context_fragment(tenant_id)
        except Exception:
            pass

        # 1d. Archivos procesados — uploads confirmados del tenant (fail-silencioso)
        file_context = ""
        try:
            file_context = await self._load_file_context(tenant_id, db)
        except Exception:
            pass

        # 2. Historial conversacional
        conversation_ctx: dict = {}
        if request.conversation_id:
            svc = ConversationService(redis, db)
            conversation_ctx = await svc.get_context(request.conversation_id)

        # 3. CEO clasifica intent
        ceo = AgentCEO()
        ceo_response = await ceo.process(request)
        target_agent_name: str = ceo_response.result.get("target_agent", "agent_helper")

        # 4. Sub-agente ejecuta lógica de negocio
        sub_agent = get_sub_agent(
            target_agent_name, db=db, redis=redis, user_id=user_id, tenant_id=tenant_id
        )
        agent_response = await sub_agent.process(request) if sub_agent is not None else ceo_response

        # 4b. requires_google_auth — propagar sin llamar LLM ni guardar turno
        if agent_response.status == "requires_google_auth":
            if not agent_response.message:
                agent_response.message = agent_response.result.get(
                    "message", "Necesito acceso a Google para continuar."
                )
            return agent_response

        # 5a. requires_approval → no llamar LLM (ahorra tokens; el summary es suficiente)
        if agent_response.requires_approval:
            agent_response.message = agent_response.result.get(
                "summary", "Requiere tu confirmación para continuar."
            )
        else:
            # 5b. success / requires_clarification → LLM Haiku genera texto rico
            try:
                agent_response.message = await self._generate_rich_response(
                    request,
                    agent_response,
                    business_name,
                    business_type,
                    heuristics,
                    conversation_ctx,
                    bm_data,
                    agent_memory_fragment,
                    file_context,
                )
            except AnthropicConfigurationError:
                raise
            except Exception as exc:
                logger.warning("chat_orchestrator_llm_failed", error=str(exc))
                agent_response.message = agent_response.result.get("summary") or "Procesado."

        # 6. Guardar turno (best-effort)
        if request.conversation_id:
            await self._save_turn(
                request, agent_response.message or "", redis, db, tenant_id, user_id
            )

        return agent_response

    # ─────────────────────────────────────────────────────────────────────────

    async def _load_business_context(
        self, tenant_id: uuid.UUID, db: AsyncSession
    ) -> tuple[str, str]:
        tenant = await db.get(Tenant, tenant_id)
        business_name = tenant.display_name if tenant else "tu negocio"

        stmt = select(BusinessProfile).where(BusinessProfile.tenant_id == tenant_id)
        result = await db.execute(stmt)
        profile = result.scalar_one_or_none()
        business_type = profile.vertical_code if profile else "kiosco_almacen"

        return business_name, business_type

    async def _generate_rich_response(
        self,
        request: AgentRequest,
        agent_response: AgentResponse,
        business_name: str,
        business_type: str,
        heuristics: object,
        conversation_ctx: dict,
        bm_data: dict | None = None,
        agent_memory_fragment: str = "",
        file_context: str = "",
    ) -> str:
        turns = conversation_ctx.get("turns", [])
        history = (
            "\n".join(f"{t['role'].upper()}: {t['content']}" for t in turns[-4:])
            or "Sin historial previo."
        )

        from app.application.agents.shared.heuristic_engine import HeuristicConfig  # noqa: PLC0415
        heuristic_fragment = (
            heuristics.to_prompt_fragment()
            if isinstance(heuristics, HeuristicConfig)
            else ""
        )

        # Fragmento de BusinessMemory: solo si hay summary generado
        memory_fragment = ""
        if bm_data:
            summary = bm_data.get("llm_context_summary")
            if summary:
                memory_fragment = f"\n\nEstado actual del negocio: {summary}"

        # Fragmento de AgentMemory: patrones aprendidos del negocio
        agent_mem_section = f"\n\n{agent_memory_fragment}" if agent_memory_fragment else ""

        # Archivos procesados: datos cargados por el usuario (márgenes, costos, etc.)
        file_section = (
            f"\n\nArchivos de referencia cargados por el usuario:\n{file_context}"
            if file_context
            else ""
        )

        system = (
            f"Sos el asistente financiero de Véktor para {business_name} ({business_type}).\n\n"
            f"{heuristic_fragment}"
            f"{memory_fragment}"
            f"{agent_mem_section}"
            f"{file_section}\n\n"
            "MISIÓN: Generá una respuesta conversacional, clara y accionable "
            "basada en los resultados del análisis.\n\n"
            "REGLAS:\n"
            "- NUNCA respondas 'Listo.' ni frases genéricas vacías.\n"
            "- Si hay datos numéricos, interpretálos en contexto del negocio.\n"
            "- Si hay alertas o sugerencias, destacalas con claridad.\n"
            "- Si el estado es 'requires_clarification', reformulá la pregunta amigablemente.\n"
            "- Máximo 3 párrafos cortos. Tono directo, español rioplatense.\n"
            f"\nHistorial reciente:\n{history}"
        )
        user_content = (
            f"Mensaje del usuario: {wrap_user_input(request.message)}\n\n"
            f"Resultado del análisis:\n{self._format_agent_result(agent_response)}"
        )

        response = await self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            system=system,
            messages=[{"role": "user", "content": user_content}],
        )
        return response.content[0].text.strip() if response.content else "Procesado."

    def _format_agent_result(self, agent_response: AgentResponse) -> str:
        result = agent_response.result
        lines: list[str] = []
        if s := result.get("summary"):
            lines.append(f"Resumen: {s}")
        if s := result.get("health_score"):
            lines.append(f"Score de salud: {s}/100")
        if comps := result.get("components"):
            if isinstance(comps, dict):
                for k, v in comps.items():
                    lines.append(
                        f"  {k}: {v:.0f}/100" if isinstance(v, float) else f"  {k}: {v}"
                    )
        if alerts := result.get("alerts"):
            if isinstance(alerts, list):
                for a in alerts[:3]:
                    if isinstance(a, dict):
                        lines.append(f"Alerta: {a.get('message', '')}")
        if q := agent_response.question:
            lines.append(f"Pregunta pendiente: {q}")
        return "\n".join(lines) or str(result)[:300]

    async def _load_file_context(self, tenant_id: uuid.UUID, db: AsyncSession) -> str:
        """Carga los últimos archivos procesados del tenant e incluye su resumen."""
        from sqlalchemy import desc, select  # noqa: PLC0415
        from app.persistence.models.file import (  # noqa: PLC0415
            PROCESSING_STATUS_DONE,
            PROCESSING_STATUS_NEEDS_CONFIRMATION,
            UploadedFile,
        )

        stmt = (
            select(UploadedFile)
            .where(
                UploadedFile.tenant_id == tenant_id,
                UploadedFile.processing_status.in_(
                    [PROCESSING_STATUS_DONE, PROCESSING_STATUS_NEEDS_CONFIRMATION]
                ),
                UploadedFile.parsed_summary_json.isnot(None),
            )
            .order_by(desc(UploadedFile.created_at))
            .limit(5)
        )
        result = await db.execute(stmt)
        files = result.scalars().all()
        if not files:
            return ""

        lines: list[str] = ["Archivos cargados por el usuario:"]
        for f in files:
            summary = f.parsed_summary_json or {}
            if not isinstance(summary, dict):
                continue
            file_type = summary.get("type", f.purpose)
            row_count = summary.get("row_count", "?")
            date_str = f.created_at.strftime("%d/%m/%Y") if f.created_at else "?"
            line = f"- {f.original_filename} ({date_str}): {file_type}, {row_count} filas"
            cols = summary.get("columns", [])
            if cols:
                line += f", columnas: {', '.join(str(c) for c in cols[:6])}"
            lines.append(line)
            for key in ("data", "rows", "products", "margins", "totals"):
                if key in summary:
                    preview = str(summary[key])[:300]
                    lines.append(f"  datos: {preview}")
                    break
        return "\n".join(lines) if len(lines) > 1 else ""

    async def _save_turn(
        self,
        request: AgentRequest,
        assistant_message: str,
        redis: Redis,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        try:
            svc = ConversationService(redis, db)
            conversation_id = request.conversation_id
            if not conversation_id:
                return
            await svc.add_turn(conversation_id, "user", request.message)
            await svc.add_turn(conversation_id, "assistant", assistant_message)
            await svc.persist(conversation_id, tenant_id, user_id)
        except Exception as exc:
            logger.warning("chat_orchestrator_save_turn_failed", error=str(exc))
