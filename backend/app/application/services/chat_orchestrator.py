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
from contextlib import suppress
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agents.ceo.agent import AgentCEO
from app.application.agents.registry import get_sub_agent
from app.application.agents.shared.heuristic_engine import HeuristicEngine
from app.application.agents.shared.schemas import AgentRequest, AgentResponse
from app.application.security.prompt_defense import wrap_user_input
from app.application.services.agent_memory_service import AgentMemoryService
from app.application.services.business_memory_service import BusinessMemoryService
from app.application.services.conversation_service import ConversationService
from app.application.services.file_parsing import (
    preview_value_from_summary,
    summary_columns,
    summary_row_count,
)
from app.integrations.anthropic_client import (
    AnthropicConfigurationError,
    get_anthropic_async_client,
)
from app.observability.logger import get_logger
from app.persistence.models.business import BusinessProfile
from app.persistence.models.file import (
    PROCESSING_STATUS_DONE,
    PROCESSING_STATUS_NEEDS_CONFIRMATION,
    UploadedFile,
)
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
        with suppress(Exception):
            file_context = await self._load_file_context(tenant_id, db, request.attachments)

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
            "\n".join(self._format_history_turn(t) for t in turns[-4:])
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
        if (comps := result.get("components")) and isinstance(comps, dict):
            for k, v in comps.items():
                lines.append(
                    f"  {k}: {v:.0f}/100" if isinstance(v, float) else f"  {k}: {v}"
                )
        if (alerts := result.get("alerts")) and isinstance(alerts, list):
            for a in alerts[:3]:
                if isinstance(a, dict):
                    lines.append(f"Alerta: {a.get('message', '')}")
        if q := agent_response.question:
            lines.append(f"Pregunta pendiente: {q}")
        return "\n".join(lines) or str(result)[:300]

    async def _load_file_context(
        self,
        tenant_id: uuid.UUID,
        db: AsyncSession,
        attachments: list[Any] | None = None,
    ) -> str:
        """Build contextual file fragments for current attachments and recent parsed files."""
        sections: list[str] = []
        attachment_files: list[UploadedFile] = []
        attachment_ids = self._extract_attachment_ids(attachments or [])

        if attachment_ids:
            stmt = (
                select(UploadedFile)
                .where(
                    UploadedFile.tenant_id == tenant_id,
                    UploadedFile.id.in_(attachment_ids),
                )
                .order_by(desc(UploadedFile.created_at))
            )
            result = await db.execute(stmt)
            loaded = list(result.scalars().all())
            files_by_id = {str(file.id): file for file in loaded}
            attachment_files = [
                files_by_id[file_id] for file_id in attachment_ids if file_id in files_by_id
            ]
            current_lines = self._render_file_lines(
                attachment_files,
                heading="Adjuntos del mensaje actual:",
            )
            if current_lines:
                sections.append(current_lines)

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
        recent_files = [
            file
            for file in result.scalars().all()
            if str(file.id) not in set(attachment_ids)
        ]
        recent_lines = self._render_file_lines(
            recent_files,
            heading="Archivos recientes del usuario:",
        )
        if recent_lines:
            sections.append(recent_lines)

        return "\n\n".join(section for section in sections if section).strip()

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
            user_metadata = self._build_turn_metadata(request.attachments)
            await svc.add_turn(
                conversation_id,
                "user",
                request.message,
                metadata=user_metadata,
            )
            await svc.add_turn(conversation_id, "assistant", assistant_message)
            await svc.persist(conversation_id, tenant_id, user_id)
        except Exception as exc:
            logger.warning("chat_orchestrator_save_turn_failed", error=str(exc))

    @staticmethod
    def _extract_attachment_ids(attachments: list[Any]) -> list[str]:
        file_ids: list[str] = []
        for attachment in attachments:
            file_id = None
            if isinstance(attachment, dict):
                file_id = attachment.get("file_id")
            else:
                file_id = getattr(attachment, "file_id", None)
            if isinstance(file_id, str) and file_id:
                file_ids.append(file_id)
        return file_ids

    @staticmethod
    def _build_turn_metadata(attachments: list[Any]) -> dict[str, Any] | None:
        normalized: list[dict[str, str]] = []
        for attachment in attachments:
            if isinstance(attachment, dict):
                file_id = attachment.get("file_id")
                filename = attachment.get("filename")
            else:
                file_id = getattr(attachment, "file_id", None)
                filename = getattr(attachment, "filename", None)
            if isinstance(file_id, str) and isinstance(filename, str):
                normalized.append({"file_id": file_id, "filename": filename})
        return {"attachments": normalized} if normalized else None

    @staticmethod
    def _format_history_turn(turn: dict[str, Any]) -> str:
        content = str(turn.get("content", ""))
        attachments = turn.get("attachments")
        if isinstance(attachments, list) and attachments:
            names = [
                str(item.get("filename"))
                for item in attachments
                if isinstance(item, dict) and item.get("filename")
            ]
            if names:
                return f"{turn['role'].upper()}: {content} [adjuntos: {', '.join(names)}]"
        return f"{turn['role'].upper()}: {content}"

    def _render_file_lines(self, files: list[UploadedFile], heading: str) -> str:
        lines: list[str] = []
        for file in files:
            summary = file.parsed_summary_json or {}
            if not isinstance(summary, dict):
                continue
            file_type = str(summary.get("file_type") or summary.get("type") or file.purpose)
            source_format = str(summary.get("source_format") or file.content_type)
            row_count = summary_row_count(summary)
            date_str = file.created_at.strftime("%d/%m/%Y") if file.created_at else "?"
            line = (
                f"- {file.original_filename} ({date_str}): {file_type}/{source_format}, "
                f"{row_count} filas"
            )
            columns = summary_columns(summary)
            if columns:
                line += f", columnas: {', '.join(columns[:6])}"
            lines.append(line)

            preview = preview_value_from_summary(summary)
            if preview:
                lines.append(f"  datos: {preview}")

            warnings = summary.get("warnings")
            if isinstance(warnings, list) and warnings:
                lines.append(f"  warning: {str(warnings[0])[:200]}")

            if error := summary.get("error"):
                lines.append(f"  error: {str(error)[:200]}")

        if not lines:
            return ""
        return "\n".join([heading, *lines])
