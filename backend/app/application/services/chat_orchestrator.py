"""ChatOrchestrator — capa conversacional entre el endpoint /agent/chat y los sub-agentes.

Flujo:
  1. Cargar contexto del negocio (nombre, tipo) + heurísticas numéricas
  2. Cargar historial de la conversación (ConversationService)
  3. AgentCEO clasifica el intent → AgentTeamPlan
  4. TeamPlanExecutor ejecuta el plan (sesiones DB aisladas por task)
  5a. Multi-task con requires_approval → respuesta agrupada (pending_action_ids + approval_group_id)
  5b. Multi-task sin approval + requires_synthesis → synthesis con Sonnet
  5c. Single-task requires_approval → summary estructurado (sin LLM adicional)
  5d. Single-task success / requires_clarification → LLM Haiku genera respuesta conversacional rica
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
from app.application.agents.ceo.synthesis import synthesize_team_results
from app.application.agents.chat.agent import AgentChat
from app.application.agents.shared.heuristic_engine import HeuristicEngine
from app.application.agents.shared.schemas import (
    AgentRequest,
    AgentResponse,
    AgentTeamPlan,
    Confidence,
    LLMCall,
    RiskLevel,
    UsageSummary,
)
from app.application.services.agent_memory_service import AgentMemoryService
from app.application.services.business_memory_service import BusinessMemoryService
from app.application.services.conversation_service import ConversationService
from app.application.services.file_parsing import (
    preview_value_from_summary,
    summary_columns,
    summary_row_count,
)
from app.application.services.team_plan_executor import TeamPlanExecutor
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


# ── Cortes determinísticos sin sub-agente (Sprint 17) ─────────────────────────
# Intents que el ChatOrchestrator resuelve directamente, sin despachar a un
# sub-agente ni gastar tokens de LLM. Son los tres cortes determinísticos:
#   - out_of_scope / intent_desconocido → fuera del scope de Véktor (ajuste #1)
#   - pedir_aclaracion_sobre_archivo    → hay adjunto pero la intención es ambigua
#   - pedir_aclaracion_negocio          → suena a negocio pero no se pudo precisar
_NO_AGENT_INTENTS: frozenset[str] = frozenset(
    {
        "out_of_scope",
        "intent_desconocido",
        "pedir_aclaracion_sobre_archivo",
        "pedir_aclaracion_negocio",
    }
)

_OUT_OF_SCOPE_MESSAGE = (
    "Véktor está especializado en la salud financiera de tu negocio. "
    "Este tema queda fuera de mis competencias. "
    "Si tenés dudas sobre ventas, gastos, stock o cómo usar la plataforma, "
    "con gusto te ayudo."
)
_ACLARACION_ARCHIVO_MESSAGE = (
    "Vi que adjuntaste un archivo, pero no estoy seguro de qué querés que haga con él. "
    "¿Querés que lo analice, lo importe, o que revise precios, márgenes o stock? "
    "Decime un poco más y lo resuelvo."
)
_ACLARACION_NEGOCIO_MESSAGE = (
    "No estoy seguro de qué querés hacer. ¿Me podés dar un poco más de detalle? "
    "Puedo ayudarte con ventas, gastos, stock, precios, márgenes, proveedores o flujo de caja."
)

_NO_AGENT_MESSAGES: dict[str, str] = {
    "out_of_scope": _OUT_OF_SCOPE_MESSAGE,
    "intent_desconocido": _OUT_OF_SCOPE_MESSAGE,
    "pedir_aclaracion_sobre_archivo": _ACLARACION_ARCHIVO_MESSAGE,
    "pedir_aclaracion_negocio": _ACLARACION_NEGOCIO_MESSAGE,
}


class ChatOrchestrator:
    def __init__(self) -> None:
        self.client: Any = get_anthropic_async_client()
        self._agent_chat = AgentChat()

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
        bm_data: dict[str, Any] = {}
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
        conversation_ctx: dict[str, Any] = {}
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
        all_llm_calls: list[LLMCall] = list(ceo_response.usage.calls) if ceo_response.usage else []
        ceo_intent: str | None = ceo_response.result.get("intent")
        ceo_target: str | None = ceo_response.result.get("target_agent")

        # 3b. Reconstruir el plan tipado desde el dict del CEO
        raw_plan: dict[str, Any] | None = ceo_response.result.get("plan")
        plan: AgentTeamPlan | None = None
        if raw_plan:
            try:
                from app.application.agents.shared.schemas import (  # noqa: PLC0415
                    ActionType,
                    AgentTask,
                )

                tasks = [
                    AgentTask(
                        task_id=td.get("task_id", ""),
                        agent=td.get("agent", "agent_helper"),
                        action_type=ActionType(td.get("action_type", "ANSWER_HELP_REQUEST")),
                        entities=td.get("entities") or {},
                        depends_on=td.get("depends_on") or [],
                        approval_group=td.get("approval_group"),
                    )
                    for td in raw_plan.get("tasks", [])
                ]
                plan = AgentTeamPlan(
                    plan_id=raw_plan.get("plan_id", ""),
                    intent=raw_plan.get("intent", ceo_intent or ""),
                    tasks=tasks,
                    requires_synthesis=raw_plan.get("requires_synthesis", False),
                    fallback_message=raw_plan.get("fallback_message"),
                )
            except Exception as exc:
                logger.warning("chat_orchestrator_plan_parse_failed", error=str(exc))

        # 3b-bis. Rescate de intent ambiguo (Sprint 17, Stages 0 + 0.5) ──────────
        # Antes de cortar, intentamos rescatar el intent con dos capas determinísticas:
        #   1. DataIntentExtractor: ¿el adjunto tiene datos importables?
        #   2. IntentRescue: scoring semántico (verbo ambiguo + objeto + contexto)
        if ceo_intent == "intent_desconocido":
            from app.application.agents.ceo.team_plan_builder import build_plan  # noqa: PLC0415

            rescued_intent, rescued_entities = await self._rescue_unknown_intent(
                request, tenant_id, db
            )
            if rescued_intent != ceo_intent:
                logger.info(
                    "chat_orchestrator_intent_rescued",
                    original="intent_desconocido",
                    rescued_intent=rescued_intent,
                    tenant_id=str(tenant_id),
                )
            ceo_intent = rescued_intent
            if rescued_intent not in _NO_AGENT_INTENTS:
                plan = build_plan(rescued_intent, rescued_entities)
                raw_plan = plan.model_dump()
                ceo_target = plan.tasks[0].agent if plan.tasks else ceo_target

        # 3c. Cortes sin sub-agente: fuera de scope / pedido de aclaración ───────
        if ceo_intent in _NO_AGENT_INTENTS:
            _summary = (
                "Consulta fuera del scope de Véktor."
                if ceo_intent in ("out_of_scope", "intent_desconocido")
                else "Se solicita aclaración al usuario."
            )
            _oos_result: dict[str, Any] = {
                "summary": _summary,
                "intent": ceo_intent,
                "target_agent": ceo_target,
            }
            if raw_plan:
                _oos_result["plan"] = raw_plan
            out_of_scope_response = AgentResponse(
                request_id=request.request_id,
                agent_name="agent_ceo",
                status="success",
                risk_level=ceo_response.risk_level,
                confidence=ceo_response.confidence,
                requires_approval=False,
                message=_NO_AGENT_MESSAGES[ceo_intent],
                result=_oos_result,
                usage=UsageSummary(calls=all_llm_calls) if all_llm_calls else None,
            )
            if request.conversation_id:
                await self._save_turn(
                    request, out_of_scope_response.message or "", redis, db, tenant_id, user_id
                )
            return out_of_scope_response

        # 4. Ejecutar plan via TeamPlanExecutor (single-task y multi-task)
        if plan is None or not plan.tasks:
            # Fallback defensivo: si no hay plan válido, crear uno single-task helper
            from app.application.agents.shared.schemas import ActionType, AgentTask  # noqa: PLC0415

            plan = AgentTeamPlan(
                plan_id="",
                intent=ceo_intent or "intent_desconocido",
                tasks=[
                    AgentTask(
                        task_id="",
                        agent="agent_helper",
                        action_type=ActionType.ANSWER_HELP_REQUEST,
                        entities={},
                    )
                ],
            )

        executor = TeamPlanExecutor(redis=redis, user_id=user_id, tenant_id=tenant_id)
        task_responses = await executor.execute(plan, request)
        for resp in task_responses:
            if resp.usage:
                all_llm_calls.extend(resp.usage.calls)

        is_multi = len(task_responses) > 1

        # 4b. Para multi-task: construir respuesta agrupada y retornar
        if is_multi:
            agent_response = self._merge_multi_task_responses(
                plan=plan,
                task_responses=task_responses,
                request=request,
                all_llm_calls=all_llm_calls,
                ceo_intent=ceo_intent,
                ceo_target=ceo_target,
                raw_plan=raw_plan,
            )
            # Si ninguna tarea requiere aprobación y el plan pide síntesis → sintetizar
            any_approval = any(r.requires_approval for r in task_responses)
            if not any_approval and plan.requires_synthesis:
                try:
                    synth_text, synth_call = await synthesize_team_results(
                        plan=plan,
                        responses=task_responses,
                        request=request,
                        business_name=business_name,
                        client=self.client,
                    )
                    agent_response.message = synth_text
                    all_llm_calls.append(synth_call)
                except Exception as exc:
                    logger.warning("chat_orchestrator_synthesis_failed", error=str(exc))
                    agent_response.message = agent_response.result.get("summary") or "Procesado."
            elif not any_approval and not agent_response.message:
                agent_response.message = (
                    agent_response.result.get("summary") or "Operaciones completadas."
                )

            agent_response.usage = UsageSummary(calls=all_llm_calls) if all_llm_calls else None
            if request.conversation_id:
                await self._save_turn(
                    request, agent_response.message or "", redis, db, tenant_id, user_id
                )
            return agent_response

        # ── Single-task: flujo heredado ───────────────────────────────────────
        agent_response = task_responses[0]

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
            if raw_plan:
                agent_response.result["plan"] = raw_plan
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
                # success / requires_clarification → AgentChat genera texto rico con Sonnet
                try:
                    agent_response.message, orch_call = await self._agent_chat.generate_response(
                        request=request,
                        agent_response=agent_response,
                        business_name=business_name,
                        business_type=business_type,
                        heuristics=heuristics,
                        conversation_ctx=conversation_ctx,
                        bm_data=bm_data,
                        agent_memory_fragment=agent_memory_fragment,
                        file_context=file_context,
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

        # 7. Preservar metadata del CEO en result para audit log
        if ceo_intent:
            agent_response.result["intent"] = ceo_intent
        if ceo_target:
            agent_response.result["target_agent"] = ceo_target
        if raw_plan:
            agent_response.result["plan"] = raw_plan
        # 7b. ayuda_plataforma via chat principal → sugerir /help
        if ceo_intent == "ayuda_plataforma" and "redirect_to" not in agent_response.result:
            agent_response.result["redirect_to"] = "help_chat"

        # 8. Adjuntar usage acumulado
        agent_response.usage = UsageSummary(calls=all_llm_calls) if all_llm_calls else None

        return agent_response

    # ─────────────────────────────────────────────────────────────────────────

    def _merge_multi_task_responses(
        self,
        plan: AgentTeamPlan,
        task_responses: list[AgentResponse],
        request: AgentRequest,
        all_llm_calls: list[LLMCall],
        ceo_intent: str | None,
        ceo_target: str | None,
        raw_plan: dict[str, Any] | None,
    ) -> AgentResponse:
        """Combina las respuestas de un plan multi-task en una sola AgentResponse.

        Para planes con requires_approval: status=requires_approval, embeds task_responses.
        Para planes exitosos: status=success con message vacío (synthesis lo llena luego).
        """
        _risk_order = {str(RiskLevel.LOW): 0, str(RiskLevel.MEDIUM): 1, str(RiskLevel.HIGH): 2}
        max_risk = max(
            task_responses,
            key=lambda r: _risk_order.get(str(r.risk_level), 0),
            default=task_responses[0],
        ).risk_level

        any_approval = any(r.requires_approval for r in task_responses)
        any_google_auth = any(r.status == "requires_google_auth" for r in task_responses)
        any_error = any(r.status == "error" for r in task_responses)

        if any_google_auth:
            # Propagar el primero con auth error
            auth_resp = next(r for r in task_responses if r.status == "requires_google_auth")
            auth_resp.result["intent"] = ceo_intent
            if raw_plan:
                auth_resp.result["plan"] = raw_plan
            return auth_resp

        if any_error and not any_approval:
            # Solo errores sin aprobaciones → reportar error
            error_summaries = [
                r.result.get("error") or r.result.get("summary") or "error"
                for r in task_responses
                if r.status == "error"
            ]
            return AgentResponse(
                request_id=request.request_id,
                agent_name="agent_ceo",
                status="error",
                risk_level=max_risk,
                confidence=Confidence.LOW,
                requires_approval=False,
                message=f"Error en las operaciones: {'; '.join(error_summaries[:2])}",
                result={
                    "intent": ceo_intent,
                    "plan": raw_plan,
                    "task_responses": [r.result for r in task_responses],
                },
            )

        # Construir summaries por task para mostrar al usuario
        task_summaries = []
        for i, (task, resp) in enumerate(zip(plan.tasks, task_responses, strict=True)):
            summary = resp.result.get("summary") or f"Tarea {i + 1} ({task.agent})"
            task_summaries.append(f"• {summary}")

        merged_summary = "\n".join(task_summaries)

        merged_result: dict[str, Any] = {
            "intent": ceo_intent,
            "target_agent": ceo_target,
            "plan": raw_plan,
            "summary": merged_summary,
            "task_responses": [
                {
                    "task_id": task.task_id,
                    "agent": r.agent_name,
                    "action_type": r.result.get("action_type") or str(task.action_type),
                    "summary": r.result.get("summary"),
                    "payload": (
                        r.result.get("structured_data")
                        or r.result.get("payload")
                        or r.result.get("entities")
                        or {}
                    ),
                    "risk_level": str(r.risk_level),
                    "requires_approval": r.requires_approval,
                    "status": r.status,
                    "tokens_input": (r.usage.total_input if r.usage else 0),
                    "tokens_output": (r.usage.total_output if r.usage else 0),
                }
                for task, r in zip(plan.tasks, task_responses, strict=True)
            ],
        }
        # Agregar fallback_message del plan si existe
        if plan.fallback_message:
            merged_result["fallback_message"] = plan.fallback_message

        return AgentResponse(
            request_id=request.request_id,
            agent_name="agent_ceo",
            status="requires_approval" if any_approval else "success",
            risk_level=max_risk,
            confidence=Confidence.HIGH,
            requires_approval=any_approval,
            message=merged_summary if any_approval else "",
            result=merged_result,
            usage=UsageSummary(calls=all_llm_calls) if all_llm_calls else None,
        )

    # ─────────────────────────────────────────────────────────────────────────

    async def _rescue_unknown_intent(
        self,
        request: AgentRequest,
        tenant_id: uuid.UUID,
        db: AsyncSession,
    ) -> tuple[str, dict[str, Any]]:
        """Rescata el intent cuando el CEO devolvió `intent_desconocido` (Sprint 17).

        Dos capas determinísticas, sin LLM ni tokens:
          1. DataIntentExtractor sobre los adjuntos ya parseados → ¿datos importables?
          2. IntentRescue.rescue_intent() → scoring semántico (verbo + objeto + contexto)

        Returns:
            (intent, entities). Si nada matchea devuelve un intent de aclaración
            (`pedir_aclaracion_sobre_archivo` / `pedir_aclaracion_negocio`) o
            `out_of_scope` — todos en `_NO_AGENT_INTENTS`. Fail-safe: ante cualquier
            error de DB, cae a un rescate basado solo en el texto del mensaje.
        """
        from app.application.agents.shared.intent_rescue import rescue_intent  # noqa: PLC0415
        from app.application.services.data_intent_extractor import (  # noqa: PLC0415
            DataIntentExtractor,
        )

        attachment_files: list[UploadedFile] = []
        try:
            attachment_files = await self._load_attachment_files(request.attachments, tenant_id, db)
        except Exception as exc:
            logger.warning(
                "intent_rescue_attachment_load_failed",
                tenant_id=str(tenant_id),
                error=str(exc),
            )

        has_attachment = bool(attachment_files)

        # ── Capa 1: DataIntentExtractor — ¿el adjunto tiene datos importables? ──
        attachment_type: str | None = None
        extractor = DataIntentExtractor()
        for file in attachment_files:
            result = extractor.check_file_summary(file.parsed_summary_json or {})
            if result.has_data_intent:
                attachment_type = result.intent_type
                # Sprint 19: intents consolidados — un adjunto de productos es un
                # análisis de lista de precios; el resto, análisis de archivo.
                if result.intent_type == "product":
                    return "analizar_precios", {"analysis_type": "lista"}
                return "analizar_archivo", {}

        # ── Capa 2: IntentRescue — scoring semántico + fuzzy sobre el texto ────
        rescued_intent, rescued_entities = rescue_intent(
            request.message,
            has_attachment=has_attachment,
            attachment_type=attachment_type,
        )
        return rescued_intent, rescued_entities

    async def _load_attachment_files(
        self,
        attachments: list[Any] | None,
        tenant_id: uuid.UUID,
        db: AsyncSession,
    ) -> list[UploadedFile]:
        """Carga los UploadedFile adjuntos al mensaje actual (con tenant isolation)."""
        attachment_ids = self._extract_attachment_ids(attachments or [])
        if not attachment_ids:
            return []
        # Convertir a uuid.UUID — el tipo UUID requiere objetos, no strings (SQLite/PG)
        uuid_ids: list[uuid.UUID] = []
        for fid in attachment_ids:
            try:
                uuid_ids.append(uuid.UUID(fid))
            except (ValueError, TypeError):
                continue
        if not uuid_ids:
            return []
        stmt = (
            select(UploadedFile)
            .where(
                UploadedFile.tenant_id == tenant_id,
                UploadedFile.id.in_(uuid_ids),
            )
            .order_by(desc(UploadedFile.created_at))
        )
        result = await db.execute(stmt)
        loaded = list(result.scalars().all())
        for _file in loaded:
            if _file.tenant_id != tenant_id:
                logger.error(
                    "intent_rescue.tenant_isolation_violation",
                    expected=str(tenant_id),
                    found=str(_file.tenant_id),
                    file_id=str(_file.id),
                )
                return []  # fail-safe: nunca rescatar usando archivos de otro tenant
        return loaded

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

    @staticmethod
    def _strip_markdown(text: str) -> str:
        text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.DOTALL)
        text = re.sub(r"\*(.+?)\*", r"\1", text, flags=re.DOTALL)
        text = re.sub(r"__(.+?)__", r"\1", text, flags=re.DOTALL)
        text = re.sub(r"_(.+?)_", r"\1", text, flags=re.DOTALL)
        text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
        return text

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
            file for file in result.scalars().all() if str(file.id) not in set(attachment_ids)
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
