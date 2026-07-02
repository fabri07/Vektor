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

import asyncio
import json
import re
import time
import uuid
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agents.ceo.agent import AgentCEO
from app.application.agents.ceo.synthesis import synthesize_team_results
from app.application.agents.ceo.team_plan_builder import INTENT_CATALOG
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

_PENDING_FILE_TTL_SECONDS = 30 * 60
_PENDING_FILE_KEY_PREFIX = "chat:pending_file:"
_FILE_INTENT_PREFIXES = ("importar_archivo_",)
_FILE_INTENTS: frozenset[str] = frozenset(
    {
        "analizar_archivo",
        "analizar_precios",
        "analizar_stock",
        "pedir_aclaracion_sobre_archivo",
    }
)
# Términos que reactivan un archivo pendiente cuando el turno actual no trae adjunto.
# IMPORTANTE: se excluyen verbos ambiguos puros como "anota"/"registra" — son los
# triggers del registro MANUAL de ventas/gastos ("anotame una venta de $1200"). Si los
# incluyéramos, un registro manual con un pending file vivo secuestraría el archivo y
# podría re-importarlo. Para esos verbos exigimos co-ocurrencia con un término de archivo
# ("archivo", "esto", "planilla", etc.), que sí está en esta lista.
_PENDING_FILE_REUSE_TERMS: frozenset[str] = frozenset(
    {
        "adjunto",
        "archivo",
        "csv",
        "excel",
        "esto",
        "estos",
        "importa",
        "importar",
        "importalo",
        "importala",
        "carga",
        "cargar",
        "cargalo",
        "cargala",
        "planilla",
        "revisa",
        "analiza",
        "mira",
        "chequea",
    }
)


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

# Umbral de confianza bajo el cual el orchestrator pide aclaración en vez de despachar
_CLARIFICATION_CONFIDENCE_THRESHOLD = 0.72

# ── Explicación de alertas del dashboard (Parte B) ────────────────────────────
# Detección determinística (sin LLM) de "explicame lo que veo en la pantalla":
# verbo de explicación + referencia deíctica a la alerta/el cartel rojo.
_ALERT_EXPLAIN_RE = re.compile(
    r"(?:explic|signific|por\s?qu[eé]|qu[eé]\s+quiere\s+decir|qu[eé]\s+es)"
    r".{0,40}?"
    r"(?:rojo|roja|alerta|cartel|aviso|mensaje|esto\s+que\s+veo|lo\s+que\s+veo)"
    r"|(?:mensaje|cartel|alerta|aviso)\s+(?:en\s+)?roj[oa]",
    re.IGNORECASE,
)

_UI_CONTEXT_MISSING_MESSAGE = (
    "Quiero explicarte exactamente lo que estás viendo, pero desde acá no sé "
    "qué alerta tenés en pantalla. Abrí el chat desde la pantalla del tablero "
    "(donde aparece el mensaje rojo) y preguntame de nuevo, así te lo explico "
    "con tus números."
)

# Nota que se inyecta en el system prompt de AgentChat cuando business_memory falló.
_DEGRADED_BUSINESS_MEMORY_NOTICE = (
    "Nota: estás respondiendo sin el contexto histórico del negocio (no se pudo cargar). "
    "Sé prudente y no afirmes datos que no tenés."
)


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
        # Cache de UploadedFile por request (evita recargar el mismo archivo en
        # _build_attachment_meta / _rescue_unknown_intent / _load_file_context). La
        # instancia de ChatOrchestrator es por-request (agent.py), así que es seguro.
        self._file_cache: dict[str, UploadedFile] = {}

        # 1. Contexto del negocio
        business_name, business_type = await self._load_business_context(tenant_id, db)
        heuristics = HeuristicEngine.get(business_type)

        current_attachments = list(request.attachments or [])
        inherited_attachments: list[dict[str, str]] = []
        if request.conversation_id:
            if current_attachments:
                await self._remember_pending_files(
                    request.conversation_id,
                    current_attachments,
                    redis,
                )
            else:
                inherited_attachments = await self._recall_pending_files(
                    request.conversation_id, redis, db, tenant_id
                )
        should_offer_inherited_attachment = bool(
            inherited_attachments
            and not current_attachments
            and self._can_reuse_pending_file(request.message)
        )
        effective_attachments: list[Any] = current_attachments or (
            inherited_attachments if should_offer_inherited_attachment else []
        )
        inherited_attachment_available = bool(
            inherited_attachments and should_offer_inherited_attachment
        )
        request.context["attachment_meta"] = await self._build_attachment_meta(
            effective_attachments,
            tenant_id,
            db,
        )

        # 1b. Observabilidad de capas de contexto: se actualiza en cada except fail-silent.
        # Capas rastreadas: business_memory, agent_memory, file_context.
        context_health: dict[str, str] = {
            "business_memory": "ok",
            "agent_memory": "ok",
            "file_context": "ok",
        }

        # 3. Lanzar CEO como asyncio.Task en paralelo con la carga de contexto.
        #
        # El CEO NO toca la DB (guard explícito en ceo/agent.py); los servicios de
        # contexto que siguen SÍ usan `db`, así que corren secuencialmente en este
        # hilo mientras el CEO corre aislado con su propio cliente Anthropic.
        # La AsyncSession NO se comparte con el task del CEO.
        # attachment_meta ya está listo en request.context (se calculó arriba).
        ceo = AgentCEO()
        ceo_task: asyncio.Task[AgentResponse] = asyncio.create_task(ceo.process(request))

        # 1b. BusinessMemory — contexto acumulado del negocio (fail-silencioso, usa db)
        bm_data: dict[str, Any] = {}
        try:
            bm_svc = BusinessMemoryService(db=db, redis=redis)
            bm_data = await bm_svc.get(tenant_id)
        except Exception as exc:
            logger.warning("business_memory_failed", tenant_id=str(tenant_id), error=str(exc))
            context_health["business_memory"] = "degraded"

        # 1c. AgentMemory — patrones aprendidos del negocio (fail-silencioso, usa db)
        agent_memory_fragment = ""
        try:
            am_svc = AgentMemoryService(db=db, redis=redis)
            agent_memory_fragment = await am_svc.get_context_fragment(tenant_id)
        except Exception as exc:
            logger.warning("agent_memory_failed", tenant_id=str(tenant_id), error=str(exc))
            context_health["agent_memory"] = "degraded"

        # 2. Historial conversacional (fail-silencioso para no dejar ceo_task huérfano
        # ante un error inesperado del ConversationService antes del await ceo_task)
        conversation_ctx: dict[str, Any] = {}
        if request.conversation_id:
            try:
                svc = ConversationService(redis, db)
                conversation_ctx = await svc.get_context(request.conversation_id)
            except Exception as exc:
                logger.warning(
                    "conversation_context_failed", tenant_id=str(tenant_id), error=str(exc)
                )

        # Esperar al CEO — siempre awaited, sin excepción posible antes de este punto
        try:
            ceo_response = await ceo_task
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
        # También rescatamos si el CEO devolvió pedir_aclaracion_sobre_archivo
        # directamente con archivos adjuntos — el rescue puede resolver mejor.
        # Y pedir_aclaracion_negocio (Workstream C4): el usuario suena a negocio pero
        # el CEO no precisó; el rescate determinístico puede mapear verbos de
        # búsqueda/reclasificación. Guarda anti-loop: el rescate corre UNA sola vez;
        # si devuelve otro sentinel de _NO_AGENT_INTENTS, se mantiene (no re-itera).
        _should_rescue = ceo_intent in ("intent_desconocido", "pedir_aclaracion_negocio") or (
            ceo_intent == "pedir_aclaracion_sobre_archivo" and bool(effective_attachments)
        )
        # ¿El rescate determinístico resolvió la ambigüedad cambiando el intent a uno
        # despachable? Si fue así, NO aplicamos el gate de confianza más abajo: el
        # `confidence_float` que trae `ceo_response.result` es el de la clasificación
        # ORIGINAL (p. ej. intent_desconocido con 0.3) y clobbearía el rescate.
        _rescue_applied = False
        if _should_rescue:
            from app.application.agents.ceo.team_plan_builder import build_plan  # noqa: PLC0415

            _original_intent = ceo_intent
            rescued_intent, rescued_entities = await self._rescue_unknown_intent(
                request,
                tenant_id,
                db,
                effective_attachments=effective_attachments if effective_attachments else None,
            )
            if rescued_intent != _original_intent:
                logger.info(
                    "chat_orchestrator_intent_rescued",
                    original=_original_intent,
                    rescued_intent=rescued_intent,
                    tenant_id=str(tenant_id),
                )
            ceo_intent = rescued_intent
            if rescued_intent not in _NO_AGENT_INTENTS:
                plan = build_plan(rescued_intent, rescued_entities)
                raw_plan = plan.model_dump()
                ceo_target = plan.tasks[0].agent if plan.tasks else ceo_target
                _rescue_applied = rescued_intent != _original_intent

        if inherited_attachment_available and self._is_file_intent(ceo_intent):
            request.attachments = inherited_attachments
        elif current_attachments:
            request.attachments = current_attachments

        # 3b-ter. Archivos procesados — solo incluir adjunto heredado si el intent sigue siendo
        # de archivo; si el usuario cambió de tema, queda vivo en Redis pero no contamina el turno.
        file_context = ""
        try:
            file_context = await self._load_file_context(tenant_id, db, request.attachments)
        except Exception as exc:
            logger.warning("file_context_failed", tenant_id=str(tenant_id), error=str(exc))
            context_health["file_context"] = "degraded"

        # 3c-pre. Explicación de alertas del dashboard (Parte B) ─────────────────
        # Señal fuerte de UI: el frontend mandó active_alert_ids y el mensaje
        # pide explicar lo que se ve → se explica con FactsService, fresco.
        # Sin ui_context, solo interviene si el mensaje iba a morir enlatado
        # (no secuestra intents despachables).
        _explain_requested = bool(_ALERT_EXPLAIN_RE.search(request.message or ""))
        _ui_alert_ids: list[str] = [
            str(a)
            for a in ((request.ui_context or {}).get("active_alert_ids") or [])
            if a
        ]
        if _explain_requested and _ui_alert_ids:
            return await self._handle_alert_explanation(
                request,
                db=db,
                redis=redis,
                tenant_id=tenant_id,
                user_id=user_id,
                alert_ids=_ui_alert_ids,
                business_name=business_name,
                business_type=business_type,
                prior_calls=all_llm_calls,
            )
        if _explain_requested and ceo_intent in _NO_AGENT_INTENTS:
            await self._log_coverage_gap(
                request,
                tenant_id=tenant_id,
                user_id=user_id,
                fallback_reason="ui_context_missing",
                classified_intent=ceo_intent,
                confidence=ceo_response.result.get("confidence_float"),
            )
            _missing_ctx_response = AgentResponse(
                request_id=request.request_id,
                agent_name="agent_ceo",
                status="success",
                risk_level=RiskLevel.LOW,
                confidence=Confidence.LOW,
                requires_approval=False,
                message=_UI_CONTEXT_MISSING_MESSAGE,
                result={
                    "summary": "Pedido de explicación de alerta sin contexto de UI.",
                    "intent": "explicar_alerta",
                    "target_agent": None,
                },
                usage=UsageSummary(calls=all_llm_calls) if all_llm_calls else None,
            )
            if request.conversation_id:
                await self._save_turn(
                    request,
                    _missing_ctx_response.message or "",
                    redis,
                    db,
                    tenant_id,
                    user_id,
                )
            return _missing_ctx_response

        # 3c. Cortes sin sub-agente: fuera de scope / pedido de aclaración ───────
        if ceo_intent in _NO_AGENT_INTENTS:
            # Coverage gap (best-effort, no cambia la respuesta): lo que llega acá
            # sobrevivió al rescate → es un gap real de cobertura, no ambigüedad
            # rescatable. pedir_aclaracion_* se registra como baja_confianza.
            await self._log_coverage_gap(
                request,
                tenant_id=tenant_id,
                user_id=user_id,
                fallback_reason=(
                    ceo_intent
                    if ceo_intent in ("out_of_scope", "intent_desconocido")
                    else "baja_confianza"
                ),
                classified_intent=ceo_intent,
                confidence=ceo_response.result.get("confidence_float"),
            )
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

        # 3d. Gate de confianza — si el CEO tuvo baja confianza, pedir aclaración
        # sin despachar al sub-agente (sin tokens adicionales de LLM).
        _confidence_float: float | None = ceo_response.result.get("confidence_float")
        if (
            not _rescue_applied
            and _confidence_float is not None
            and _confidence_float < _CLARIFICATION_CONFIDENCE_THRESHOLD
            and ceo_intent is not None
            and ceo_intent not in _NO_AGENT_INTENTS
        ):
            # Coverage gap (best-effort): el CEO clasificó pero sin confianza para
            # despachar — el fraseo del usuario no matchea bien ningún intent.
            await self._log_coverage_gap(
                request,
                tenant_id=tenant_id,
                user_id=user_id,
                fallback_reason="baja_confianza",
                classified_intent=ceo_intent,
                confidence=_confidence_float,
            )
            _amb_raw = ceo_response.result.get("ambiguous_with")
            _ambiguous_with: list[str] = _amb_raw if _amb_raw is not None else []

            def _intent_friendly(key: str) -> str:
                """Devuelve la descripción legible del intent o el key crudo como fallback."""
                entry = INTENT_CATALOG.get(key, {})
                raw_desc = entry.get("desc")
                return str(raw_desc) if raw_desc is not None else key

            if _ambiguous_with:
                _main_label = _intent_friendly(ceo_intent)
                _alts = [_intent_friendly(str(a)) for a in _ambiguous_with]
                if len(_alts) == 1:
                    _question = (
                        f"¿Querés {_main_label} o {_alts[0]}? "
                        "Decímelo con un poco más de detalle y lo hago."
                    )
                else:
                    _alts_str = ", ".join(_alts[:-1]) + f" o {_alts[-1]}"
                    _question = (
                        f"¿Querés {_main_label} o {_alts_str}? "
                        "Decímelo con un poco más de detalle y lo hago."
                    )
            else:
                _question = (
                    "No entendí bien qué querés hacer. "
                    "¿Me podés dar más detalle sobre lo que necesitás?"
                )
            _clarification_response = AgentResponse(
                request_id=request.request_id,
                agent_name="agent_ceo",
                status="requires_clarification",
                risk_level=ceo_response.risk_level,
                confidence=Confidence.LOW,
                requires_approval=False,
                question=_question,
                result={
                    "summary": "Se solicita aclaración al usuario.",
                    "intent": ceo_intent,
                    "target_agent": ceo_target,
                    "confidence_float": _confidence_float,
                },
                usage=UsageSummary(calls=all_llm_calls) if all_llm_calls else None,
            )
            if request.conversation_id:
                await self._save_turn(
                    request, _question, redis, db, tenant_id, user_id
                )
            return _clarification_response

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
                _degraded_notice: str | None = (
                    _DEGRADED_BUSINESS_MEMORY_NOTICE
                    if context_health["business_memory"] == "degraded"
                    else None
                )
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
                        degraded_notice=_degraded_notice,
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

        # 7c. context_health — observabilidad de capas de contexto (additive, sin migración)
        if agent_response.result is None:
            agent_response.result = {}
        agent_response.result["context_health"] = context_health

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
        effective_attachments: list[Any] | None = None,
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
            attachment_files = await self._load_attachment_files(
                effective_attachments if effective_attachments is not None else request.attachments,
                tenant_id,
                db,
            )
        except Exception as exc:
            logger.warning(
                "intent_rescue_attachment_load_failed",
                tenant_id=str(tenant_id),
                error=str(exc),
            )

        has_attachment = bool(attachment_files)

        # ── Capa 1: DataIntentExtractor — solo extrae tipo; IntentRescue decide import vs análisis.
        attachment_type: str | None = None
        extractor = DataIntentExtractor()
        for file in attachment_files:
            result = extractor.check_file_summary(file.parsed_summary_json or {})
            if result.has_data_intent:
                attachment_type = result.intent_type
                break

        # ── Capa 2: IntentRescue — scoring semántico + fuzzy sobre el texto ────
        rescued_intent, rescued_entities = rescue_intent(
            request.message,
            has_attachment=has_attachment,
            attachment_type=attachment_type,
        )
        return rescued_intent, rescued_entities

    async def _build_attachment_meta(
        self,
        attachments: list[Any] | None,
        tenant_id: uuid.UUID,
        db: AsyncSession,
    ) -> dict[str, Any]:
        if not attachments:
            return {"has_attachment": False, "attachment_type": None}
        attachment_type: str | None = None
        try:
            from app.application.services.data_intent_extractor import (  # noqa: PLC0415
                DataIntentExtractor,
            )

            files = await self._load_attachment_files(attachments, tenant_id, db)
            extractor = DataIntentExtractor()
            for file in files:
                result = extractor.check_file_summary(file.parsed_summary_json or {})
                if result.has_data_intent:
                    attachment_type = result.intent_type
                    break
        except Exception as exc:
            logger.warning("attachment_meta_failed", tenant_id=str(tenant_id), error=str(exc))
        return {"has_attachment": bool(attachments), "attachment_type": attachment_type}

    async def _remember_pending_files(
        self,
        conversation_id: str,
        attachments: list[Any],
        redis: Redis,
    ) -> None:
        normalized = self._normalize_attachments(attachments)
        if not normalized:
            return
        payload = {
            "file_ids": [item["file_id"] for item in normalized],
            "attachments": normalized,
            "ts": int(time.time()),
        }
        try:
            await redis.setex(
                self._pending_file_key(conversation_id),
                _PENDING_FILE_TTL_SECONDS,
                json.dumps(payload),
            )
        except Exception as exc:
            logger.warning(
                "pending_file_remember_failed",
                conversation_id=conversation_id,
                error=str(exc),
            )

    async def _recall_pending_files(
        self,
        conversation_id: str,
        redis: Redis,
        db: AsyncSession,
        tenant_id: uuid.UUID,
    ) -> list[dict[str, str]]:
        try:
            raw = await redis.get(self._pending_file_key(conversation_id))
        except Exception as exc:
            logger.warning(
                "pending_file_recall_failed",
                conversation_id=conversation_id,
                error=str(exc),
            )
            return []
        if not raw:
            return []
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return []
        attachments = payload.get("attachments")
        if isinstance(attachments, list):
            recalled = self._normalize_attachments(attachments)
        else:
            file_ids = payload.get("file_ids")
            recalled = (
                [
                    {"file_id": file_id, "filename": ""}
                    for file_id in file_ids
                    if isinstance(file_id, str) and file_id
                ]
                if isinstance(file_ids, list)
                else []
            )
        if not recalled:
            return []
        # #1(a): no re-ofrecer archivos YA importados. Un chat upload nace con
        # processing_status=DONE (parseo síncrono), así que ese campo no distingue
        # "parseado" de "importado". El marcador real es `imported_counts` en el
        # summary, que escribe pending_action_service al confirmar IMPORT_TABULAR_FILE.
        return await self._filter_already_imported(recalled, tenant_id, db)

    async def _filter_already_imported(
        self,
        attachments: list[dict[str, str]],
        tenant_id: uuid.UUID,
        db: AsyncSession,
    ) -> list[dict[str, str]]:
        try:
            files = await self._load_attachment_files(attachments, tenant_id, db)
        except Exception as exc:
            logger.warning(
                "pending_file_import_filter_failed",
                tenant_id=str(tenant_id),
                error=str(exc),
            )
            return attachments
        imported_ids = {
            str(f.id)
            for f in files
            if isinstance(f.parsed_summary_json, dict)
            and f.parsed_summary_json.get("imported_counts")
        }
        if not imported_ids:
            return attachments
        return [a for a in attachments if a.get("file_id") not in imported_ids]

    @staticmethod
    def _pending_file_key(conversation_id: str) -> str:
        return f"{_PENDING_FILE_KEY_PREFIX}{conversation_id}"

    @staticmethod
    def _is_file_intent(intent: str | None) -> bool:
        if not intent:
            return False
        return intent in _FILE_INTENTS or intent.startswith(_FILE_INTENT_PREFIXES)

    @staticmethod
    def _can_reuse_pending_file(message: str) -> bool:
        try:
            from app.application.agents.shared.intent_rescue import normalize  # noqa: PLC0415

            normalized = normalize(message)
        except Exception:
            normalized = message.lower()
        return any(term in normalized for term in _PENDING_FILE_REUSE_TERMS)

    async def _load_attachment_files(
        self,
        attachments: list[Any] | None,
        tenant_id: uuid.UUID,
        db: AsyncSession,
    ) -> list[UploadedFile]:
        """Carga los UploadedFile adjuntos al mensaje actual (con tenant isolation).

        Usa un cache por-request (`self._file_cache`) para no re-consultar el mismo
        archivo en cada paso del pipeline (#5). El cache lo inicializa `handle()`.
        """
        attachment_ids = self._extract_attachment_ids(attachments or [])
        if not attachment_ids:
            return []

        cache: dict[str, UploadedFile] = getattr(self, "_file_cache", {})
        missing_ids = [fid for fid in attachment_ids if fid not in cache]

        if missing_ids:
            # Convertir a uuid.UUID — el tipo UUID requiere objetos, no strings (SQLite/PG)
            uuid_ids: list[uuid.UUID] = []
            for fid in missing_ids:
                try:
                    uuid_ids.append(uuid.UUID(fid))
                except (ValueError, TypeError):
                    continue
            if uuid_ids:
                stmt = (
                    select(UploadedFile)
                    .where(
                        UploadedFile.tenant_id == tenant_id,
                        UploadedFile.id.in_(uuid_ids),
                    )
                    .order_by(desc(UploadedFile.created_at))
                )
                result = await db.execute(stmt)
                for _file in result.scalars().all():
                    if _file.tenant_id != tenant_id:
                        logger.error(
                            "intent_rescue.tenant_isolation_violation",
                            expected=str(tenant_id),
                            found=str(_file.tenant_id),
                            file_id=str(_file.id),
                        )
                        return []  # fail-safe: nunca usar archivos de otro tenant
                    cache[str(_file.id)] = _file

        # Devolver en el orden de los attachments pedidos, solo los que existen.
        return [cache[fid] for fid in attachment_ids if fid in cache]

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

    async def _handle_alert_explanation(
        self,
        request: AgentRequest,
        *,
        db: AsyncSession,
        redis: Redis,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        alert_ids: list[str],
        business_name: str,
        business_type: str,
        prior_calls: list[LLMCall],
    ) -> AgentResponse:
        """Explica el/los alert(s) del dashboard con el número fresco de FactsService.

        Compone, no calcula: FactsService trae el BusinessFact (nunca cacheado),
        alert_explainer aporta el significado funcional y redacta en llano.
        """
        from app.application.agents.shared.alert_explainer import (  # noqa: PLC0415
            explain_alerts,
            resolve_alert_facts,
        )
        from app.application.services.facts_provider import (  # noqa: PLC0415
            build_facts_service,
        )
        from app.application.services.facts_service import Period  # noqa: PLC0415

        period = Period.last_n_days(30)
        blocks: list[dict[str, Any]] = []
        try:
            facts_service = await build_facts_service(
                db, tenant_id, period, vertical_code=business_type
            )
            blocks = resolve_alert_facts(facts_service, str(tenant_id), alert_ids, period)
        except Exception as exc:
            logger.warning(
                "alert_explanation_facts_failed",
                error=str(exc),
                tenant_id=str(tenant_id),
            )

        if not blocks:
            # Ids que no pudimos resolver a ningún dato: honestidad + backlog.
            await self._log_coverage_gap(
                request,
                tenant_id=tenant_id,
                user_id=user_id,
                fallback_reason="sin_datos",
                classified_intent="explicar_alerta",
            )
            response = AgentResponse(
                request_id=request.request_id,
                agent_name="agent_ceo",
                status="success",
                risk_level=RiskLevel.LOW,
                confidence=Confidence.LOW,
                requires_approval=False,
                message=(
                    "Veo que preguntás por una alerta del tablero, pero no encontré "
                    "los datos que la generaron. Puede que haya cambiado hace un "
                    "momento. Actualizá la pantalla y, si sigue apareciendo, "
                    "preguntame de nuevo."
                ),
                result={
                    "summary": "Alerta no resuelta a datos.",
                    "intent": "explicar_alerta",
                    "target_agent": None,
                    "alert_ids": alert_ids,
                },
                usage=UsageSummary(calls=prior_calls) if prior_calls else None,
            )
        else:
            if any(b.get("fact") is None for b in blocks):
                # Alerta sin BusinessFact que la respalde (ej. SUPPLIER_DEPENDENCY):
                # se explica funcionalmente igual, pero queda como gap de producto.
                await self._log_coverage_gap(
                    request,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    fallback_reason="sin_datos",
                    classified_intent="explicar_alerta",
                )
            text, llm_call = await explain_alerts(
                request.message, blocks, business_name, self.client
            )
            response = AgentResponse(
                request_id=request.request_id,
                agent_name="agent_ceo",
                status="success",
                risk_level=RiskLevel.LOW,
                confidence=Confidence.HIGH,
                requires_approval=False,
                message=text,
                result={
                    "summary": "Explicación de alerta del dashboard.",
                    "intent": "explicar_alerta",
                    "target_agent": None,
                    "alert_explanation": True,
                    "alert_ids": alert_ids,
                },
                usage=UsageSummary(calls=[*prior_calls, llm_call]),
            )

        if request.conversation_id:
            await self._save_turn(
                request, response.message or "", redis, db, tenant_id, user_id
            )
        return response

    async def _log_coverage_gap(
        self,
        request: AgentRequest,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID | None,
        fallback_reason: str,
        classified_intent: str | None = None,
        classified_domain: str | None = None,
        confidence: float | None = None,
    ) -> None:
        """Registra el rechazo como gap de producto. Best-effort: nunca lanza,
        nunca cambia la respuesta al usuario (CoverageGapService ya es fail-silent
        y usa sesión propia; este try/except es el cinturón extra del hot path)."""
        try:
            from app.application.services.coverage_gap_service import (  # noqa: PLC0415
                CoverageGapService,
            )

            await CoverageGapService().log_gap(
                tenant_id=tenant_id,
                user_id=user_id,
                original_message=request.message,
                fallback_reason=fallback_reason,
                classified_intent=classified_intent,
                classified_domain=classified_domain,
                confidence=confidence,
                ui_context=getattr(request, "ui_context", None),
            )
        except Exception as exc:
            logger.warning("coverage_gap_hook_failed", error=str(exc))

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
        return [item["file_id"] for item in ChatOrchestrator._normalize_attachments(attachments)]

    @staticmethod
    def _build_turn_metadata(attachments: list[Any]) -> dict[str, Any] | None:
        normalized = ChatOrchestrator._normalize_attachments(attachments)
        return {"attachments": normalized} if normalized else None

    @staticmethod
    def _normalize_attachments(attachments: list[Any]) -> list[dict[str, str]]:
        normalized: list[dict[str, str]] = []
        for attachment in attachments:
            if isinstance(attachment, dict):
                file_id = attachment.get("file_id")
                filename = attachment.get("filename")
            else:
                file_id = getattr(attachment, "file_id", None)
                filename = getattr(attachment, "filename", None)
            if isinstance(file_id, str) and file_id:
                item = {
                    "file_id": file_id,
                    "filename": filename if isinstance(filename, str) else "",
                }
                normalized.append(item)
        return normalized

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
