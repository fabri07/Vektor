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

import re
import uuid
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agents.ceo.agent import AgentCEO
from app.application.agents.registry import get_sub_agent
from app.application.agents.shared.heuristic_engine import HeuristicEngine
from app.application.agents.shared.schemas import AgentRequest, AgentResponse, LLMCall, UsageSummary
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
        except Exception as exc:
            logger.warning("business_memory_failed", tenant_id=str(tenant_id), error=str(exc))

        # 1c. AgentMemory — patrones aprendidos del negocio (fail-silencioso)
        agent_memory_fragment = ""
        try:
            am_svc = AgentMemoryService(db=db, redis=redis)
            agent_memory_fragment = await am_svc.get_context_fragment(tenant_id)
        except Exception as exc:
            logger.warning("agent_memory_failed", tenant_id=str(tenant_id), error=str(exc))

        # 1d. Archivos procesados — uploads confirmados del tenant (fail-silencioso)
        file_context = ""
        try:
            file_context = await self._load_file_context(tenant_id, db, request.attachments)
        except Exception as exc:
            logger.warning("file_context_failed", tenant_id=str(tenant_id), error=str(exc))

        # 2. Historial conversacional
        conversation_ctx: dict = {}
        if request.conversation_id:
            svc = ConversationService(redis, db)
            conversation_ctx = await svc.get_context(request.conversation_id)

        # 3. CEO clasifica intent
        ceo = AgentCEO()
        try:
            ceo_response = await ceo.process(request)
        except AnthropicConfigurationError:
            raise
        except Exception as exc:
            logger.error(
                "chat_orchestrator_ceo_failed",
                error=str(exc),
                error_type=type(exc).__name__,
                tenant_id=str(tenant_id),
                exc_info=True,
            )
            return AgentResponse(
                request_id=request.request_id,
                agent_name="agent_ceo",
                status="error",
                risk_level="LOW",
                confidence="LOW",
                requires_approval=False,
                message="No pude clasificar tu mensaje. Por favor intentá de nuevo.",
                result={"summary": "CEO classification failed"},
            )
        target_agent_name: str = ceo_response.result.get("target_agent", "agent_helper")
        all_llm_calls: list[LLMCall] = list(ceo_response.usage.calls) if ceo_response.usage else []
        ceo_intent: str | None = ceo_response.result.get("intent")
        ceo_target: str | None = ceo_response.result.get("target_agent")

        # 3b. out_of_scope — cortar sin sub-agente ni LLM adicional
        if ceo_response.result.get("intent") == "out_of_scope":
            out_of_scope_response = AgentResponse(
                request_id=request.request_id,
                agent_name="agent_ceo",
                status="success",
                risk_level=ceo_response.risk_level,
                confidence=ceo_response.confidence,
                requires_approval=False,
                message=(
                    "Véktor está especializado en la salud financiera de tu negocio. "
                    "Este tema queda fuera de mis competencias. "
                    "Si tenés dudas sobre ventas, gastos, stock o cómo usar la plataforma, "
                    "con gusto te ayudo."
                ),
                result={"summary": "Consulta fuera del scope de Véktor.", "intent": "out_of_scope"},
                usage=UsageSummary(calls=all_llm_calls) if all_llm_calls else None,
            )
            if request.conversation_id:
                await self._save_turn(
                    request, out_of_scope_response.message or "", redis, db, tenant_id, user_id
                )
            return out_of_scope_response

        # 4. Sub-agente ejecuta lógica de negocio
        sub_agent = get_sub_agent(
            target_agent_name, db=db, redis=redis, user_id=user_id, tenant_id=tenant_id
        )
        if sub_agent is None:
            logger.error(
                "chat_orchestrator_agent_not_found",
                target_agent=target_agent_name,
                intent=ceo_intent,
                tenant_id=str(tenant_id),
            )
            # Fallback: AgentHelper responde de forma genérica sin exponer el error interno
            from app.application.agents.helper.agent import AgentHelper  # noqa: PLC0415
            sub_agent = AgentHelper()
        agent_response = await sub_agent.process(request)
        if agent_response.usage:
            all_llm_calls.extend(agent_response.usage.calls)

        # 4b. requires_google_auth — propagar sin llamar LLM ni guardar turno
        if agent_response.status == "requires_google_auth":
            if not agent_response.message:
                agent_response.message = agent_response.result.get(
                    "message", "Necesito acceso a Google para continuar."
                )
            if ceo_intent:
                agent_response.result["intent"] = ceo_intent
            if ceo_target:
                agent_response.result["target_agent"] = ceo_target
            agent_response.usage = UsageSummary(calls=all_llm_calls) if all_llm_calls else None
            return agent_response

        # 5a. requires_approval → no llamar LLM (ahorra tokens; el summary es suficiente)
        if agent_response.requires_approval:
            agent_response.message = agent_response.result.get(
                "summary", "Requiere tu confirmación para continuar."
            )
        else:
            # 5b. Si el agente ya generó su propia respuesta (ej. AgentHealth), usarla directo
            if agent_response.message:
                agent_response.message = self._strip_markdown(agent_response.message)
            else:
                # success / requires_clarification → LLM Haiku genera texto rico
                try:
                    agent_response.message, orch_call = await self._generate_rich_response(
                        request,
                        agent_response,
                        business_name,
                        business_type,
                        heuristics,
                        conversation_ctx,
                        bm_data,
                        agent_memory_fragment,
                        file_context,
                        tenant_id=tenant_id,
                        db=db,
                        redis=redis,
                    )
                    all_llm_calls.append(orch_call)
                except AnthropicConfigurationError:
                    logger.error(
                        "chat_orchestrator_anthropic_not_configured",
                        tenant_id=str(tenant_id),
                        exc_info=True,
                    )
                    agent_response.message = (
                        "El servicio de IA no está disponible temporalmente. "
                        "Por favor intentá de nuevo más tarde."
                    )
                    agent_response.status = "error"
                except Exception as exc:
                    logger.warning("chat_orchestrator_llm_failed", error=str(exc))
                    agent_response.message = agent_response.result.get("summary") or "Procesado."

        # 6. Guardar turno (best-effort)
        if request.conversation_id:
            await self._save_turn(
                request, agent_response.message or "", redis, db, tenant_id, user_id
            )

        # 7. Preservar metadata del CEO en result para audit log (intent + agente destino)
        # Usar asignación directa para que el CEO siempre sea la fuente de verdad de intent/target.
        if ceo_intent:
            agent_response.result["intent"] = ceo_intent
        if ceo_target:
            agent_response.result["target_agent"] = ceo_target

        # 8. Adjuntar usage acumulado de todas las llamadas LLM del turno
        agent_response.usage = UsageSummary(calls=all_llm_calls) if all_llm_calls else None

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
        tenant_id: uuid.UUID | None = None,
        db: AsyncSession | None = None,
        redis: Redis | None = None,
    ) -> tuple[str, LLMCall]:
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

        # Datos financieros determinísticos del tenant actual — null-safe
        memory_fragment = ""
        try:
            from app.application.services.deterministic_finance import (  # noqa: PLC0415
                get_financial_summary,
            )
            fin_data = await get_financial_summary(tenant_id, db)
            if fin_data.get("estado") == "SIN_DATOS":
                memory_fragment = (
                    "\n\nDatos del negocio: SIN_DATOS. "
                    "INSTRUCCIÓN CRÍTICA: NO INVENTES NÚMEROS. "
                    "Indicá al usuario que debe cargar sus datos de ventas y gastos para ver análisis."
                )
            else:
                flujo = fin_data.get("flujo_neto_30d", {})
                margen = fin_data.get("margen", {})
                ticket = fin_data.get("ticket_promedio", {})
                lines: list[str] = ["\n\nDatos financieros del negocio (últimos 30 días):"]
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
                memory_fragment = "\n".join(lines)
        except Exception as exc:
            logger.warning("deterministic_finance_failed", tenant_id=str(tenant_id), error=str(exc))
            # Fail-closed: no fallback a BusinessMemory porque podría estar desactualizado.
            memory_fragment = (
                "\n\nDatos del negocio: SIN_DATOS (error temporal). "
                "INSTRUCCIÓN CRÍTICA: NO INVENTES NÚMEROS. "
                "Indicá al usuario que intente nuevamente."
            )

        try:
            if tenant_id is not None and db is not None and redis is not None:
                from app.application.services.chat_memory_service import (  # noqa: PLC0415
                    ChatMemoryService,
                )

                session_ctx = await ChatMemoryService().get_session_context_cached(
                    db,
                    redis,
                    tenant_id,
                )
                memory_fragment += "\n\n" + self._render_session_memory(session_ctx)
        except Exception as exc:
            logger.warning("chat_session_memory_failed", tenant_id=str(tenant_id), error=str(exc))
            memory_fragment += (
                "\n\nHISTORIAL DE CARGAS: No disponible temporalmente. "
                "No inventes información histórica."
            )

        # Fragmento de AgentMemory: patrones aprendidos del negocio
        agent_mem_section = f"\n\n{agent_memory_fragment}" if agent_memory_fragment else ""

        # Archivos procesados: datos cargados por el usuario (márgenes, costos, etc.)
        file_section = (
            f"\n\nArchivos de referencia cargados por el usuario:\n{file_context}"
            if file_context
            else ""
        )

        action_type = agent_response.result.get("action_type", "")
        action_executed = bool(agent_response.result.get("auto_executed")) or bool(
            agent_response.result.get("pending_action_id")
        )
        action_ctx = f"Acción determinada: {action_type}\n\n" if action_type else ""

        no_action_rule = (
            "REGLA CRÍTICA: No se ejecutó ninguna operación en esta respuesta. "
            "NO afirmes que registraste, eliminaste, corregiste ni modificaste ningún dato. "
            "Respondé solo con información o con instrucciones para que el usuario tome acción.\n\n"
            if not action_type
            else (
                "REGLA CRÍTICA: Esta operación AÚN NO se ejecutó — el usuario debe confirmarla primero. "
                "NO digas que ya fue registrada ni realizada.\n\n"
                if not action_executed
                else ""
            )
        )

        system = (
            f"Sos el asistente financiero de Véktor para {business_name} ({business_type}).\n\n"
            "CAPACIDADES DE VÉKTOR: registro de ventas, gastos, compras y movimientos de caja; "
            "modificar productos (precio, costo, umbral de stock, activar/desactivar, renombrar); "
            "ajustar stock e inventario; preparar borradores de email a proveedores (Gmail); "
            "clasificar mensajes recibidos de proveedores; "
            "crear y consultar eventos en Google Calendar; sincronizar con Google Sheets y Docs.\n\n"
            "FUENTE DE VERDAD — REGLA CRÍTICA: Véktor es el único lugar donde se modifican los "
            "datos del negocio. NUNCA sugerirle al usuario que vaya a Google Sheets ni ningún "
            "servicio externo a modificar datos. Google Drive/Sheets se usa únicamente para "
            "importar datos externos hacia Véktor o generar copias opcionales de exportación — "
            "nunca como fuente de modificación.\n\n"
            f"{no_action_rule}"
            f"{action_ctx}"
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
            "- NO uses formato markdown (sin **, sin __, sin #, sin - listas). Solo texto plano.\n"
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
        orch_call = LLMCall(
            source="orchestrator",
            model="claude-haiku-4-5-20251001",
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        raw = response.content[0].text.strip() if response.content else "Procesado."
        return self._strip_markdown(raw), orch_call

    @staticmethod
    def _strip_markdown(text: str) -> str:
        text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.DOTALL)
        text = re.sub(r"\*(.+?)\*", r"\1", text, flags=re.DOTALL)
        text = re.sub(r"__(.+?)__", r"\1", text, flags=re.DOTALL)
        text = re.sub(r"_(.+?)_", r"\1", text, flags=re.DOTALL)
        text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
        return text

    def _format_agent_result(self, agent_response: AgentResponse) -> str:
        result = agent_response.result
        lines: list[str] = []
        # Nota: auto_executed y pending_action_id se setean en la capa HTTP
        # DESPUÉS de que orchestrator.handle() retorna, por lo que aquí son
        # siempre None/False. El estado se comunica al Haiku vía no_action_rule
        # en el system prompt, que sí tiene la información disponible.

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

        # Datos estructurados de la operación (truncados)
        structured = (
            result.get("structured_data")
            or result.get("entities")
            or result.get("payload")
        )
        if isinstance(structured, dict):
            relevant = {
                k: v for k, v in structured.items()
                if k not in ("conversation_id", "mode", "email_mode") and v is not None
            }
            if relevant:
                lines.append(f"Datos de la operación: {str(relevant)[:300]}")

        return "\n".join(lines) or str(result)[:300]

    @staticmethod
    def _render_session_memory(ctx: dict[str, Any]) -> str:
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
            # Tenant isolation check — todos los archivos deben pertenecer al tenant activo
            for _file in loaded:
                if _file.tenant_id != tenant_id:
                    logger.error(
                        "orchestrator.tenant_isolation_violation",
                        expected=str(tenant_id),
                        found=str(_file.tenant_id),
                        file_id=str(_file.id),
                    )
                    raise ValueError(
                        f"Tenant isolation violation in file context: "
                        f"file {_file.id} belongs to {_file.tenant_id}, not {tenant_id}"
                    )
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
