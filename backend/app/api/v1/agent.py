"""Endpoints del sistema multiagente de Véktor.

GET  /api/v1/agent/usage     — uso de mensajes del día actual (para el contador frontend)
POST /api/v1/agent/chat      — procesa mensaje, crea pending_action si MEDIUM/HIGH
POST /api/v1/agent/confirm/{pending_id}       — confirma y ejecuta una acción pendiente
POST /api/v1/agent/cancel/{pending_id}        — rechaza una acción pendiente
POST /api/v1/agent/confirm/group/{group_id}  — confirma todas las PAs de un grupo (Stage 3)
POST /api/v1/agent/cancel/group/{group_id}   — rechaza todas las PAs PENDING de un grupo (Stage 3)
GET  /api/v1/agent/conversations             — lista conversaciones del tenant (últimas 30)
GET  /api/v1/agent/conversations/{id}        — turnos completos de una conversación
"""

import asyncio
import hashlib
import json as json_module
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime, time
from typing import Any

import anthropic
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user, require_role
from app.application.agents.shared.schemas import ActionType, AgentRequest, AgentResponse
from app.application.services.automation_service import (
    AUTOMATION_PAYLOAD_AGENT_KEY,
    automation_offer_for_action,
    determine_external_system,
    find_enabled_rule,
)
from app.application.services.chat_orchestrator import ChatOrchestrator
from app.application.services.pending_action_service import (
    cancel_pending_action,
    create_pending_action,
    execute_pending_action,
)
from app.config.settings import get_settings
from app.integrations.anthropic_client import AnthropicConfigurationError
from app.integrations.mcp.exceptions import McpToolAuthError
from app.observability.logger import get_logger
from app.persistence.db.redis_client import get_redis
from app.persistence.db.session import get_db_session
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.conversation_context import AgentConversationContext
from app.persistence.models.pending_action import PendingAction
from app.persistence.models.user import User

router = APIRouter()
logger = get_logger(__name__)

_AUTO_EXECUTE_ACTION_TYPES: set[str] = {str(ActionType.REGISTER_EXPENSE)}
_FINGERPRINT_ACTION_TYPES = {
    str(ActionType.REGISTER_SALE),
    str(ActionType.REGISTER_EXPENSE),
    str(ActionType.REGISTER_PURCHASE),
}

_ANTHROPIC_RATE_LIMIT_MESSAGE = (
    "El servicio de IA alcanzó temporalmente su límite. Intenta de nuevo en unos segundos."
)
_ANTHROPIC_UNAVAILABLE_MESSAGE = (
    "El servicio de IA está saturado temporalmente. Intenta de nuevo en unos segundos."
)



class ChatRequest(BaseModel):
    message: str
    attachments: list[Any] = []
    conversation_id: str | None = None


class ConversationSummary(BaseModel):
    conversation_id: str
    title: str
    updated_at: str


class ConversationTurns(BaseModel):
    conversation_id: str
    turns: list[dict[str, Any]]


def _anthropic_error_response(exc: Exception) -> tuple[int, str] | None:
    status_code = getattr(exc, "status_code", None)

    if (
        isinstance(exc, anthropic.RateLimitError)
        or status_code == status.HTTP_429_TOO_MANY_REQUESTS
    ):
        return status.HTTP_429_TOO_MANY_REQUESTS, _ANTHROPIC_RATE_LIMIT_MESSAGE

    if isinstance(
        exc,
        anthropic.InternalServerError
        | anthropic.APIConnectionError
        | anthropic.APITimeoutError,
    ) or status_code in {
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        status.HTTP_502_BAD_GATEWAY,
        status.HTTP_503_SERVICE_UNAVAILABLE,
        status.HTTP_504_GATEWAY_TIMEOUT,
        529,
    }:
        return status.HTTP_503_SERVICE_UNAVAILABLE, _ANTHROPIC_UNAVAILABLE_MESSAGE

    return None


def _log_provider_failure(*, event: str, exc: Exception, tenant_id: uuid.UUID) -> None:
    error_body = getattr(exc, "body", None)
    request_id = error_body.get("request_id") if isinstance(error_body, dict) else None
    provider_status = getattr(exc, "status_code", None)

    logger.warning(
        event,
        error=str(exc),
        error_type=type(exc).__name__,
        provider_status_code=provider_status,
        provider_request_id=request_id,
        tenant_id=str(tenant_id),
    )


def _derive_title(turns: list[Any]) -> str:
    for turn in turns:
        if isinstance(turn, dict) and turn.get("role") == "user":
            content = str(turn.get("content", "")).strip()
            return content[:60] if content else "Conversación"
    return "Conversación"


def _extract_action_payload(agent_response: AgentResponse) -> dict[str, Any]:
    result = agent_response.result or {}
    payload = (
        result.get("structured_data")
        or result.get("payload")
        or result.get("entities")
        or {}
    )
    if not isinstance(payload, dict):
        payload = {}

    merged_payload = dict(payload)
    for key in ("mode", "sync_type"):
        if key in result and key not in merged_payload:
            merged_payload[key] = result[key]
    return merged_payload


def _attach_conversation_id(agent_response: AgentResponse, conversation_id: str | None) -> None:
    if not conversation_id:
        return
    result = agent_response.result or {}
    for key in ("structured_data", "payload", "entities"):
        payload = result.get(key)
        if isinstance(payload, dict):
            payload.setdefault("conversation_id", conversation_id)
            agent_response.result = result
            return

    result["structured_data"] = {"conversation_id": conversation_id}
    agent_response.result = result


def _topological_position_map(plan: dict[str, Any] | None) -> dict[str, int]:
    """Resuelve task_id → posición global por orden topológico.

    Itera por niveles (tasks listas con dependencias satisfechas) y asigna
    posiciones crecientes. Tasks paralelas dentro de un nivel quedan
    contiguas; tasks con dependencias quedan después de las que dependen.
    Tasks no presentes en el plan reciben posición sentinela alta.
    """
    if not isinstance(plan, dict):
        return {}
    tasks = plan.get("tasks") or []
    if not isinstance(tasks, list) or not tasks:
        return {}

    remaining = [
        {"task_id": str(t.get("task_id") or ""), "depends_on": list(t.get("depends_on") or [])}
        for t in tasks
        if t.get("task_id")
    ]
    completed: set[str] = set()
    position_map: dict[str, int] = {}
    cursor = 0

    while remaining:
        ready = [t for t in remaining if all(d in completed for d in t["depends_on"])]
        if not ready:
            # Ciclo o dependencia inválida: emitimos lo restante en orden recibido
            ready = remaining[:]
        for t in ready:
            position_map[t["task_id"]] = cursor
            cursor += 1
            completed.add(t["task_id"])
        ready_ids = {t["task_id"] for t in ready}
        remaining = [t for t in remaining if t["task_id"] not in ready_ids]

    return position_map


def _build_tasks_audit(
    agent_response: AgentResponse,
    action_meta: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Construye decision_data.tasks para el audit log.

    Multi-task: lee result["task_responses"] (enriquecido por el orchestrator).
    Single-task: deriva un único elemento desde el agent_response.
    """
    task_responses = agent_response.result.get("task_responses")
    if isinstance(task_responses, list) and task_responses:
        return [
            {
                "task_id": tr.get("task_id"),
                "agent": tr.get("agent"),
                "action_type": tr.get("action_type"),
                "status": tr.get("status"),
                "risk_level": tr.get("risk_level"),
                "requires_approval": tr.get("requires_approval"),
                "pending_action_id": tr.get("pending_action_id"),
                "tokens_input": int(tr.get("tokens_input") or 0),
                "tokens_output": int(tr.get("tokens_output") or 0),
            }
            for tr in task_responses
        ]

    # Single-task: derivar entrada desde la AgentResponse
    usage = agent_response.usage
    return [
        {
            "task_id": None,
            "agent": agent_response.agent_name,
            "action_type": agent_response.result.get("action_type"),
            "status": agent_response.status,
            "risk_level": str(agent_response.risk_level),
            "requires_approval": agent_response.requires_approval,
            "pending_action_id": agent_response.pending_action_id
            or (action_meta or {}).get("action_id"),
            "tokens_input": usage.total_input if usage else 0,
            "tokens_output": usage.total_output if usage else 0,
        }
    ]


def _payload_for_fingerprint(payload: dict[str, Any]) -> dict[str, Any]:
    structured = payload.get("structured_data")
    if isinstance(structured, dict):
        return structured
    return payload


async def _register_operation_fingerprint(
    *,
    action: PendingAction,
    tenant_id: uuid.UUID,
    db: AsyncSession,
) -> bool:
    if str(action.action_type) not in _FINGERPRINT_ACTION_TYPES:
        return False

    from sqlalchemy.exc import IntegrityError  # noqa: PLC0415

    from app.persistence.models.memory import OperationFingerprint  # noqa: PLC0415

    payload = _payload_for_fingerprint(action.payload or {})
    raw = (
        f"{tenant_id}:{action.action_type}"
        f":{payload.get('amount', '')}:{payload.get('transaction_date', payload.get('date', ''))}"
    )
    fingerprint = hashlib.sha256(raw.encode()).hexdigest()

    # Usar savepoint para que un IntegrityError no aborte la transacción padre.
    try:
        async with db.begin_nested():
            db.add(
                OperationFingerprint(
                    tenant_id=tenant_id,
                    fingerprint=fingerprint,
                    action_type=str(action.action_type),
                )
            )
    except IntegrityError:
        # El fingerprint ya existía — operación duplicada.
        return True

    return False


async def _execute_local_action(
    *,
    action: PendingAction,
    tenant_id: uuid.UUID,
    db: AsyncSession,
    redis: Redis,
) -> dict[str, Any] | None:
    if await _register_operation_fingerprint(action=action, tenant_id=tenant_id, db=db):
        action.execution_status = "SUCCEEDED"
        action.executed_at = datetime.now(UTC)
        return {
            "status": "duplicate",
            "message": "Esta operación ya fue registrada.",
            "execution_status": "DUPLICATE",
        }

    # Savepoint: si execute_pending_action falla a mitad, el rollback es atómico
    # y no deja registros huérfanos (ej. expense persistida con status=FAILED).
    try:
        async with db.begin_nested():
            await execute_pending_action(action, db, redis=redis)
        action.execution_status = "SUCCEEDED"
        action.executed_at = datetime.now(UTC)
        return None
    except Exception as exc:
        action.execution_status = "FAILED"
        action.executed_at = datetime.now(UTC)
        logger.error(
            "execute_local_action_failed",
            action_id=str(action.id),
            action_type=str(action.action_type),
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
        )
        return {
            "status": "failed",
            "message": "No se pudo completar la operación. Intente nuevamente.",
            "execution_status": "FAILED",
        }


async def _process_agent_action(
    *,
    agent_response: AgentResponse,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    db: AsyncSession,
    redis: Redis,
) -> dict[str, Any] | None:
    if agent_response.status == "error":
        return None

    # ── Multi-task: crear PendingActions agrupadas (Stage 3) ──────────────────
    task_response_list = agent_response.result.get("task_responses")
    if task_response_list is not None:
        if not agent_response.requires_approval:
            return None

        approval_group_id = str(uuid.uuid4())
        pending_ids: list[str] = []
        plan_dict = agent_response.result.get("plan") if isinstance(agent_response.result, dict) else None
        topo_position = _topological_position_map(plan_dict)
        for fallback_position, tr in enumerate(task_response_list):
            if not tr.get("requires_approval"):
                tr["pending_action_id"] = None
                continue
            action_type_tr = str(tr.get("action_type") or ActionType.ANSWER_HELP_REQUEST)
            payload_tr = dict(tr.get("payload") or {})
            payload_tr.setdefault(AUTOMATION_PAYLOAD_AGENT_KEY, str(tr.get("agent") or ""))
            risk_level_tr = str(tr.get("risk_level") or "MEDIUM")

            pending = await create_pending_action(
                db=db,
                tenant_id=tenant_id,
                user_id=user_id,
                action_type=action_type_tr,
                payload=payload_tr,
                risk_level=risk_level_tr,
            )
            pending.approval_group_id = approval_group_id
            pending.group_execution_status = "PENDING"
            # group_position desde el orden topológico del DAG (fallback al índice del array)
            tr_task_id = str(tr.get("task_id") or "")
            pending.group_position = topo_position.get(tr_task_id, fallback_position)
            pending_ids.append(str(pending.id))
            tr["pending_action_id"] = str(pending.id)

        if pending_ids:
            agent_response.pending_action_ids = pending_ids
            agent_response.approval_group_id = approval_group_id
            return {"action_id": pending_ids[0], "approval_group_id": approval_group_id}
        return None

    # ── Single-task ───────────────────────────────────────────────────────────
    action_type = str(agent_response.result.get("action_type", ActionType.ANSWER_HELP_REQUEST))
    payload = _extract_action_payload(agent_response)
    payload.setdefault(AUTOMATION_PAYLOAD_AGENT_KEY, agent_response.agent_name)
    external_system = (
        determine_external_system(action_type, payload)
        if payload.get("mode") == "mcp"
        else None
    )
    auto_execute = action_type in _AUTO_EXECUTE_ACTION_TYPES

    settings = get_settings()
    automation_rule = None
    if settings.ENABLE_AGENT_AUTOMATIONS and agent_response.requires_approval:
        automation_rule = await find_enabled_rule(
            db=db,
            tenant_id=tenant_id,
            user_id=user_id,
            agent_name=agent_response.agent_name,
            action_type=action_type,
            payload=payload,
            external_system=external_system,
        )
        auto_execute = automation_rule is not None

    if auto_execute:
        action = await create_pending_action(
            db=db,
            tenant_id=tenant_id,
            user_id=user_id,
            action_type=action_type,
            payload=payload,
            risk_level=str(agent_response.risk_level),
        )
        action.status = "APPROVED"
        action.approved_at = datetime.now(UTC)

        duplicate: dict[str, Any] | None = None
        if action.is_external:
            action.execution_status = "IN_PROGRESS"
            await db.flush()
            try:
                await execute_pending_action(action, db, redis=redis)
                action.execution_status = "SUCCEEDED"
            except McpToolAuthError as exc:
                action.execution_status = "REQUIRES_RECONNECT"
                action.failure_code = exc.reason
                action.failure_message = None
            except Exception as exc:
                action.execution_status = "FAILED"
                action.failure_code = None
                action.failure_message = str(exc)[:500]
            action.executed_at = datetime.now(UTC)
        else:
            duplicate = await _execute_local_action(
                action=action,
                tenant_id=tenant_id,
                db=db,
                redis=redis,
            )

        agent_response.status = "success"
        agent_response.requires_approval = False
        agent_response.pending_action_id = None
        agent_response.result["auto_executed"] = True
        agent_response.result["executed_action_id"] = str(action.id)
        if automation_rule is not None:
            automation_rule.last_executed_at = datetime.now(UTC)
            agent_response.result["automation_rule_id"] = str(automation_rule.id)
            db.add(
                DecisionAuditLog(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    decision_type="AUTOMATION_RULE_EXECUTED",
                    decision_data={
                        "rule_id": str(automation_rule.id),
                        "rule_key": automation_rule.rule_key,
                        "pending_action_id": str(action.id),
                        "execution_status": action.execution_status,
                    },
                    triggered_by="agent:automation",
                    actor_user_id=user_id,
                    context={"agent_name": agent_response.agent_name},
                    created_at=datetime.now(UTC),
                )
            )
        if duplicate is not None:
            agent_response.message = duplicate["message"]
            agent_response.result["summary"] = duplicate["message"]
            if duplicate.get("execution_status") == "FAILED":
                agent_response.status = "error"
                agent_response.requires_approval = False
                agent_response.result["auto_executed"] = False
                agent_response.result["execution_status"] = "FAILED"
        elif action.execution_status == "REQUIRES_RECONNECT":
            agent_response.status = "requires_google_auth"
            agent_response.result["message"] = (
                "Necesito que reconectes Google para completar esta automatización."
            )
        elif action.execution_status == "FAILED":
            agent_response.status = "error"
            agent_response.result["summary"] = "No se pudo completar la automatización."
        elif action_type == str(ActionType.REGISTER_EXPENSE):
            summary = (
                agent_response.message
                or agent_response.result.get("summary")
                or "Gasto registrado."
            )
            if not str(summary).lower().startswith("gasto registrado"):
                summary = f"Gasto registrado. {summary}"
            agent_response.message = str(summary)
            agent_response.result["summary"] = str(summary)
        return {"action_id": str(action.id)}

    if agent_response.requires_approval:
        pending = await create_pending_action(
            db=db,
            tenant_id=tenant_id,
            user_id=user_id,
            action_type=action_type,
            payload=payload,
            risk_level=str(agent_response.risk_level),
        )
        agent_response.pending_action_id = str(pending.id)
        return {"action_id": str(pending.id)}

    return None


# ── GET /usage ────────────────────────────────────────────────────────────────


@router.get("/usage", summary="Uso de mensajes del día actual")
async def get_chat_usage(
    current_user: User = Depends(get_current_user),
    redis: Redis = Depends(get_redis),
) -> dict:
    rate_key = f"rate:chat:{current_user.tenant_id}:{date.today()}"
    count = await redis.get(rate_key)
    return {"messages_today": int(count) if count else 0, "limit": 50}


# ── GET /conversations ────────────────────────────────────────────────────────


@router.get(
    "/conversations",
    response_model=list[ConversationSummary],
    summary="Listar conversaciones",
)
async def list_conversations(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> list[ConversationSummary]:
    stmt = (
        select(AgentConversationContext)
        .where(AgentConversationContext.tenant_id == current_user.tenant_id)
        .order_by(AgentConversationContext.updated_at.desc())
        .limit(30)
    )
    rows = (await db.execute(stmt)).scalars().all()
    return [
        ConversationSummary(
            conversation_id=str(row.conversation_id),
            title=_derive_title(row.turns),
            updated_at=row.updated_at.isoformat(),
        )
        for row in rows
    ]


# ── GET /conversations/{conversation_id} ──────────────────────────────────────


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationTurns,
    summary="Detalle de conversación",
)
async def get_conversation(
    conversation_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> ConversationTurns:
    row = await db.get(AgentConversationContext, conversation_id)
    if row is None or row.tenant_id != current_user.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversación no encontrada.",
        )
    return ConversationTurns(
        conversation_id=str(row.conversation_id),
        turns=row.turns,
    )


# ── POST /chat ────────────────────────────────────────────────────────────────


@router.post("/chat", response_model=AgentResponse, summary="Enviar mensaje al agente")
async def chat(
    body: ChatRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    redis: Redis = Depends(get_redis),
) -> AgentResponse:
    tenant_id = current_user.tenant_id
    user_id = current_user.user_id

    # ── Rate limit: 50 mensajes/día por tenant ────────────────────────────────
    # El check precede al orquestador; el incr se hace DESPUÉS de respuesta exitosa
    # para no consumir cuota en errores 5xx ajenos al usuario.
    # Si Redis cae, se permite el request (fail-open) con warning.
    rate_key = f"rate:chat:{tenant_id}:{date.today()}"
    try:
        current_count = await redis.get(rate_key)
        if current_count is not None and int(current_count) >= 50:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Límite diario de 50 mensajes alcanzado. Disponible mañana.",
            )
    except HTTPException:
        raise
    except Exception as redis_exc:
        logger.warning(
            "chat_rate_limit_redis_failed",
            error=str(redis_exc),
            tenant_id=str(tenant_id),
        )

    # ── ChatOrchestrator: CEO + sub-agente + LLM conversacional ─────────────
    request = AgentRequest(
        user_id=str(user_id),
        business_id=str(tenant_id),
        message=body.message,
        attachments=body.attachments,
        conversation_id=body.conversation_id,
    )
    try:
        orchestrator = ChatOrchestrator()
        agent_response = await asyncio.wait_for(
            orchestrator.handle(request, db, redis, user_id, tenant_id),
            timeout=45.0,
        )
    except TimeoutError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="El procesamiento tardó demasiado. Por favor intentá de nuevo.",
        )
    except AnthropicConfigurationError as exc:
        logger.error("agent_anthropic_not_configured", tenant_id=str(tenant_id))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="El servicio de IA no está configurado. Falta ANTHROPIC_API_KEY.",
        ) from exc
    except Exception as exc:
        anthropic_error = _anthropic_error_response(exc)
        if anthropic_error is not None:
            http_status, message = anthropic_error
            _log_provider_failure(
                event="chat_orchestrator_provider_unavailable",
                exc=exc,
                tenant_id=tenant_id,
            )
            raise HTTPException(status_code=http_status, detail=message) from exc

        logger.error(
            "chat_orchestrator_failed",
            error=str(exc),
            error_type=type(exc).__name__,
            tenant_id=str(tenant_id),
        )
        raise HTTPException(
            status_code=500, detail=f"Orchestrator error: {type(exc).__name__}: {exc}"
        ) from exc

    _attach_conversation_id(agent_response, body.conversation_id)
    action_meta = await _process_agent_action(
        agent_response=agent_response,
        tenant_id=tenant_id,
        user_id=user_id,
        db=db,
        redis=redis,
    )

    # ── Incrementar contador solo si el turno terminó sin error controlado ─────
    if agent_response.status != "error":
        try:
            count = await redis.incr(rate_key)
            if count == 1:
                midnight = datetime.combine(date.today(), time.max).replace(tzinfo=UTC)
                await redis.expireat(rate_key, midnight)
        except Exception as redis_exc:
            logger.warning(
                "chat_rate_limit_incr_failed",
                error=str(redis_exc),
                tenant_id=str(tenant_id),
            )

    # ── Audit log (insert-only) ───────────────────────────────────────────────
    _usage = agent_response.usage
    _tasks_audit = _build_tasks_audit(agent_response, action_meta)
    audit = DecisionAuditLog(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        decision_type="AGENT_CHAT",
        decision_data={
            "request_id": agent_response.request_id,
            "intent": agent_response.result.get("intent"),
            "action_type": agent_response.result.get("action_type"),
            "status": agent_response.status,
            "risk_level": str(agent_response.risk_level),
            "requires_approval": agent_response.requires_approval,
            "pending_action_id": agent_response.pending_action_id
            or (action_meta or {}).get("action_id"),
            "approval_group_id": agent_response.approval_group_id
            or (action_meta or {}).get("approval_group_id"),
            "ceo_target_agent": agent_response.result.get("target_agent"),
            "sub_agent_name": agent_response.agent_name,
            "plan_id": (agent_response.result.get("plan") or {}).get("plan_id"),
            "plan": agent_response.result.get("plan"),
            "tasks": _tasks_audit,
            "token_calls": [c.model_dump() for c in _usage.calls] if _usage else [],
        },
        triggered_by="agent:chat",
        actor_user_id=user_id,
        context={"message_length": len(body.message)},
        tokens_input=_usage.total_input if _usage else 0,
        tokens_output=_usage.total_output if _usage else 0,
        tokens_total=_usage.total if _usage else 0,
        created_at=datetime.now(UTC),
    )
    db.add(audit)
    await db.commit()

    return agent_response


# ── POST /chat/stream ────────────────────────────────────────────────────────


@router.post("/chat/stream", summary="Chat con streaming SSE")
async def chat_stream(
    body: ChatRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    redis: Redis = Depends(get_redis),
) -> StreamingResponse:
    """Endpoint SSE para chat con streaming.

    Emite tres tipos de eventos:
    - ``{"type": "thinking", "text": "..."}`` — inmediatamente al recibir el request
    - ``{"type": "response", "data": {...}}`` — AgentResponse serializada
    - ``{"type": "error", "message": "..."}`` — si hay excepción
    Finaliza con ``data: [DONE]``.
    """
    tenant_id = current_user.tenant_id
    user_id = current_user.user_id

    # Rate limit compartido con /chat — check previo al stream, incr dentro del generator
    rate_key = f"rate:chat:{tenant_id}:{date.today()}"
    try:
        current_count = await redis.get(rate_key)
        if current_count is not None and int(current_count) >= 50:
            async def _rate_limited() -> AsyncGenerator[str, None]:
                err = {
                    "type": "error",
                    "message": "Límite diario de 50 mensajes alcanzado. Disponible mañana.",
                    "code": 429,
                }
                yield f"data: {json_module.dumps(err, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(
                _rate_limited(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
    except Exception as redis_exc:
        logger.warning(
            "chat_stream_rate_limit_redis_failed",
            error=str(redis_exc),
            tenant_id=str(tenant_id),
        )

    request_obj = AgentRequest(
        user_id=str(user_id),
        business_id=str(tenant_id),
        message=body.message,
        attachments=body.attachments,
        conversation_id=body.conversation_id,
    )

    async def event_generator() -> AsyncGenerator[str, None]:
        # 1. Emitir "thinking" inmediatamente
        thinking_event = {"type": "thinking", "text": "Analizando tu mensaje..."}
        yield f"data: {json_module.dumps(thinking_event, ensure_ascii=False)}\n\n"

        try:
            orchestrator = ChatOrchestrator()
            try:
                agent_response = await asyncio.wait_for(
                    orchestrator.handle(
                        request=request_obj,
                        db=db,
                        redis=redis,
                        user_id=user_id,
                        tenant_id=tenant_id,
                    ),
                    timeout=45.0,
                )
            except TimeoutError:
                error_event = {
                    "type": "error",
                    "message": "El procesamiento tardó demasiado. Por favor intentá de nuevo.",
                    "code": 503,
                }
                yield f"data: {json_module.dumps(error_event, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
                return

            _attach_conversation_id(agent_response, body.conversation_id)
            action_meta = await _process_agent_action(
                agent_response=agent_response,
                tenant_id=tenant_id,
                user_id=user_id,
                db=db,
                redis=redis,
            )

            # 3. Audit log (insert-only)
            _usage_s = agent_response.usage
            _tasks_audit_s = _build_tasks_audit(agent_response, action_meta)
            audit = DecisionAuditLog(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                decision_type="AGENT_CHAT_STREAM",
                decision_data={
                    "request_id": agent_response.request_id,
                    "intent": agent_response.result.get("intent"),
                    "action_type": agent_response.result.get("action_type"),
                    "status": agent_response.status,
                    "risk_level": str(agent_response.risk_level),
                    "requires_approval": agent_response.requires_approval,
                    "pending_action_id": agent_response.pending_action_id
                    or (action_meta or {}).get("action_id"),
                    "approval_group_id": agent_response.approval_group_id
                    or (action_meta or {}).get("approval_group_id"),
                    "ceo_target_agent": agent_response.result.get("target_agent"),
                    "sub_agent_name": agent_response.agent_name,
                    "plan_id": (agent_response.result.get("plan") or {}).get("plan_id"),
                    "plan": agent_response.result.get("plan"),
                    "tasks": _tasks_audit_s,
                    "token_calls": [c.model_dump() for c in _usage_s.calls] if _usage_s else [],
                },
                triggered_by="agent:chat:stream",
                actor_user_id=user_id,
                context={"message_length": len(body.message)},
                tokens_input=_usage_s.total_input if _usage_s else 0,
                tokens_output=_usage_s.total_output if _usage_s else 0,
                tokens_total=_usage_s.total if _usage_s else 0,
                created_at=datetime.now(UTC),
            )
            db.add(audit)
            await db.commit()

            # Incrementar rate limit solo si el turno terminó sin error controlado
            if agent_response.status != "error":
                try:
                    _stream_count = await redis.incr(rate_key)
                    if _stream_count == 1:
                        _midnight = datetime.combine(date.today(), time.max).replace(tzinfo=UTC)
                        await redis.expireat(rate_key, _midnight)
                except Exception as _redis_exc:
                    logger.warning(
                        "chat_stream_rate_limit_incr_failed",
                        error=str(_redis_exc),
                        tenant_id=str(tenant_id),
                    )

            # 4. Emitir respuesta final
            response_event = {
                "type": "response",
                "data": agent_response.model_dump(),
            }
            yield f"data: {json_module.dumps(response_event, ensure_ascii=False)}\n\n"

        except AnthropicConfigurationError:
            error_event = {
                "type": "error",
                "message": "El servicio de IA no está configurado. Falta ANTHROPIC_API_KEY.",
                "code": 503,
            }
            yield f"data: {json_module.dumps(error_event, ensure_ascii=False)}\n\n"

        except Exception as exc:
            anthropic_error = _anthropic_error_response(exc)
            if anthropic_error is not None:
                http_status, message = anthropic_error
                _log_provider_failure(
                    event="chat_stream_provider_unavailable",
                    exc=exc,
                    tenant_id=tenant_id,
                )
                error_event = {
                    "type": "error",
                    "message": message,
                    "code": http_status,
                }
                yield f"data: {json_module.dumps(error_event, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
                return

            logger.error(
                "chat_stream_failed",
                error=str(exc),
                error_type=type(exc).__name__,
                tenant_id=str(tenant_id),
            )
            error_event = {"type": "error", "message": str(exc)}
            yield f"data: {json_module.dumps(error_event, ensure_ascii=False)}\n\n"

        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── POST /confirm/{pending_id} ────────────────────────────────────────────────


@router.post("/confirm/{pending_id}", summary="Confirmar acción pendiente")
async def confirm_action(
    pending_id: uuid.UUID,
    current_user: User = Depends(require_role("OWNER", "ADMIN")),
    db: AsyncSession = Depends(get_db_session),
    redis: Redis = Depends(get_redis),
) -> dict:
    # SELECT FOR UPDATE — previene race conditions
    stmt = (
        select(PendingAction)
        .where(
            PendingAction.id == pending_id,
            PendingAction.tenant_id == current_user.tenant_id,
            PendingAction.status == "PENDING",
        )
        .with_for_update()
    )
    result = await db.execute(stmt)
    action = result.scalar_one_or_none()

    if action is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Acción no encontrada o ya procesada.",
        )

    if action.expires_at.replace(tzinfo=UTC) < datetime.now(UTC):
        action.status = "EXPIRED"
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Esta acción venció. Volvé a enviar el mensaje.",
        )

    # ── Aprobación ────────────────────────────────────────────────────────────
    action.approved_at = datetime.now(UTC)
    action.status = "APPROVED"

    if action.is_external:
        # Acciones externas: lifecycle IN_PROGRESS → SUCCEEDED|FAILED|REQUIRES_RECONNECT.
        # Nunca se re-lanza la excepción — el fallo se persiste y el endpoint responde con estado.
        action.execution_status = "IN_PROGRESS"
        await db.flush()  # visible antes de la llamada externa
        try:
            await execute_pending_action(action, db, redis=redis)
            action.execution_status = "SUCCEEDED"
        except McpToolAuthError as exc:
            action.execution_status = "REQUIRES_RECONNECT"
            action.failure_code = exc.reason
            action.failure_message = None
        except Exception as exc:
            action.execution_status = "FAILED"
            action.failure_code = None
            action.failure_message = str(exc)[:500]
    else:
        # Deduplicación por fingerprint SHA-256 antes de ejecutar acciones financieras.
        duplicate = await _execute_local_action(
            action=action,
            tenant_id=current_user.tenant_id,
            db=db,
            redis=redis,
        )
        if duplicate is not None:
            return duplicate

    action.executed_at = datetime.now(UTC)
    await db.commit()

    logger.info(
        "pending_action_confirmed",
        action_id=str(pending_id),
        action_type=action.action_type,
        execution_status=action.execution_status,
        tenant_id=str(current_user.tenant_id),
    )
    response: dict[str, Any] = {
        "status": "confirmed",
        "action_type": action.action_type,
        "execution_status": action.execution_status,
        "failure_code": action.failure_code,
    }
    if (
        get_settings().ENABLE_AGENT_AUTOMATIONS
        and action.execution_status == "SUCCEEDED"
    ):
        response["automation_offer"] = automation_offer_for_action(action)
    return response


# ── POST /cancel/{pending_id} ─────────────────────────────────────────────────


@router.post("/cancel/{pending_id}", summary="Cancelar acción pendiente")
async def cancel_action(
    pending_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    stmt = select(PendingAction).where(
        PendingAction.id == pending_id,
        PendingAction.tenant_id == current_user.tenant_id,
    )
    result = await db.execute(stmt)
    action = result.scalar_one_or_none()

    if action is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Acción no encontrada.",
        )

    if action.status != "PENDING":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"La acción ya fue procesada (status={action.status}).",
        )

    await cancel_pending_action(action, db)
    action.status = "REJECTED"
    await db.commit()

    return {"status": "cancelled", "action_type": action.action_type}


# ── POST /confirm/group/{group_id} ───────────────────────────────────────────


@router.post("/confirm/group/{group_id}", summary="Confirmar grupo de acciones pendientes")
async def confirm_action_group(
    group_id: str,
    current_user: User = Depends(require_role("OWNER", "ADMIN")),
    db: AsyncSession = Depends(get_db_session),
    redis: Redis = Depends(get_redis),
) -> dict:
    """Aprueba todas las PendingActions del grupo y las ejecuta en orden por group_position.

    Semántica abort-all: si alguna task falla, las posteriores se marcan FAILED con
    failure_message="upstream_task_failed" y NO se ejecutan. Cada task commitea su
    propia transacción para no perder progreso ante un fallo intermedio.
    group_execution_status final → SUCCEEDED | PARTIAL_FAILED.
    """
    stmt = (
        select(PendingAction)
        .where(
            PendingAction.tenant_id == current_user.tenant_id,
            PendingAction.approval_group_id == group_id,
            PendingAction.status == "PENDING",
        )
        .order_by(PendingAction.group_position)
        .with_for_update()
    )
    result = await db.execute(stmt)
    actions = list(result.scalars().all())

    if not actions:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Grupo no encontrado o ya procesado.",
        )

    now = datetime.now(UTC)
    expired = [a for a in actions if a.expires_at.replace(tzinfo=UTC) < now]
    if expired:
        for a in expired:
            a.status = "EXPIRED"
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Una o más acciones del grupo vencieron. Volvé a enviar el mensaje.",
        )

    # Aprobamos todas en un solo commit (precondición de ejecución)
    for action in actions:
        action.status = "APPROVED"
        action.approved_at = now
        action.group_execution_status = "IN_PROGRESS"
    await db.commit()

    task_results: list[dict[str, Any]] = []
    any_failed = False
    sorted_actions = sorted(actions, key=lambda a: (a.group_position or 0))

    for action in sorted_actions:
        # Abort-all: si una previa falló, esta queda en FAILED sin ejecutar
        if any_failed:
            action.execution_status = "FAILED"
            action.failure_message = "upstream_task_failed"
            action.executed_at = now
            await db.commit()
            task_results.append({
                "action_id": str(action.id),
                "action_type": action.action_type,
                "execution_status": action.execution_status,
            })
            continue

        if action.is_external:
            action.execution_status = "IN_PROGRESS"
            await db.commit()
            try:
                await execute_pending_action(action, db, redis=redis)
                action.execution_status = "SUCCEEDED"
            except McpToolAuthError as exc:
                action.execution_status = "REQUIRES_RECONNECT"
                action.failure_code = exc.reason
                any_failed = True
            except Exception as exc:
                action.execution_status = "FAILED"
                action.failure_message = str(exc)[:500]
                any_failed = True
        else:
            duplicate = await _execute_local_action(
                action=action,
                tenant_id=current_user.tenant_id,
                db=db,
                redis=redis,
            )
            if duplicate and duplicate.get("execution_status") == "FAILED":
                any_failed = True

        action.executed_at = now
        # Commit por task: garantiza durabilidad de cada paso completado
        await db.commit()
        task_results.append({
            "action_id": str(action.id),
            "action_type": action.action_type,
            "execution_status": action.execution_status,
        })

    group_execution_status = "PARTIAL_FAILED" if any_failed else "SUCCEEDED"
    for action in actions:
        action.group_execution_status = group_execution_status

    await db.commit()

    logger.info(
        "pending_action_group_confirmed",
        group_id=group_id,
        action_count=len(actions),
        group_execution_status=group_execution_status,
        tenant_id=str(current_user.tenant_id),
    )
    return {
        "status": "confirmed",
        "approval_group_id": group_id,
        "group_execution_status": group_execution_status,
        "tasks": task_results,
    }


# ── POST /cancel/group/{group_id} ─────────────────────────────────────────────


@router.post("/cancel/group/{group_id}", summary="Cancelar grupo de acciones pendientes")
async def cancel_action_group(
    group_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """Rechaza todas las PendingActions PENDING del grupo en un solo commit."""
    stmt = select(PendingAction).where(
        PendingAction.tenant_id == current_user.tenant_id,
        PendingAction.approval_group_id == group_id,
        PendingAction.status == "PENDING",
    )
    result = await db.execute(stmt)
    actions = list(result.scalars().all())

    if not actions:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Grupo no encontrado o ya procesado.",
        )

    for action in actions:
        action.status = "REJECTED"
        action.group_execution_status = "FAILED"

    await db.commit()
    return {
        "status": "cancelled",
        "approval_group_id": group_id,
        "cancelled_count": len(actions),
    }


# ── POST /retry/{pending_id} ──────────────────────────────────────────────────


@router.post("/retry/{pending_id}", summary="Reintentar acción fallida")
async def retry_action(
    pending_id: uuid.UUID,
    current_user: User = Depends(require_role("OWNER", "ADMIN")),
    db: AsyncSession = Depends(get_db_session),
    redis: Redis = Depends(get_redis),
) -> dict:
    """Re-ejecuta una acción APPROVED cuya ejecución externa falló.

    Condiciones de elegibilidad:
    - status = "APPROVED"
    - execution_status in ("FAILED", "REQUIRES_RECONNECT")
    - No existe aún un registro AGENT_ACTION_RETRIED en DecisionAuditLog (límite: 1 retry).

    El idempotency_key original no se regenera.
    """
    # SELECT FOR UPDATE — solo acciones APPROVED con fallo de ejecución
    stmt = (
        select(PendingAction)
        .where(
            PendingAction.id == pending_id,
            PendingAction.tenant_id == current_user.tenant_id,
            PendingAction.status == "APPROVED",
            PendingAction.execution_status.in_(["FAILED", "REQUIRES_RECONNECT"]),
        )
        .with_for_update()
    )
    result = await db.execute(stmt)
    action = result.scalar_one_or_none()

    if action is None:
        # Distinguir: ¿ya fue ejecutada exitosamente?
        succeeded_stmt = select(PendingAction).where(
            PendingAction.id == pending_id,
            PendingAction.tenant_id == current_user.tenant_id,
            PendingAction.status == "APPROVED",
            PendingAction.execution_status == "SUCCEEDED",
        )
        succeeded = (await db.execute(succeeded_stmt)).scalar_one_or_none()
        if succeeded is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="La acción ya fue ejecutada exitosamente.",
            )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Acción no encontrada o no reintentable.",
        )

    # ── Guard explícito: solo acciones externas son reintentables ─────────────
    # Por diseño, solo las acciones con external_system pueden terminar en
    # FAILED|REQUIRES_RECONNECT. Este guard hace el contrato explícito en lugar
    # de depender del invariante implícito.
    if not action.is_external:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"La acción '{action.action_type}' no es reintentable "
                "(acción local sin sistema externo)."
            ),
        )

    # ── Verificar límite de 1 retry via DecisionAuditLog ─────────────────────
    # Carga solo los registros del tenant con decision_type=AGENT_ACTION_RETRIED;
    # el volumen por tenant es bajo. Filtramos pending_action_id en Python para
    # evitar queries JSON que difieren entre SQLite (tests) y PostgreSQL (prod).
    retry_logs_stmt = select(DecisionAuditLog).where(
        DecisionAuditLog.tenant_id == action.tenant_id,
        DecisionAuditLog.decision_type == "AGENT_ACTION_RETRIED",
    )
    retry_logs = (await db.execute(retry_logs_stmt)).scalars().all()
    retry_count = sum(
        1 for row in retry_logs
        if row.decision_data.get("pending_action_id") == str(action.id)
    )
    if retry_count >= 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Límite de reintentos alcanzado (máximo 1).",
        )

    # ── Limpiar estado previo e iniciar retry ─────────────────────────────────
    action.failure_code = None
    action.failure_message = None
    action.execution_status = "IN_PROGRESS"
    await db.flush()

    try:
        await execute_pending_action(action, db, redis=redis)
        action.execution_status = "SUCCEEDED"
    except McpToolAuthError as exc:
        action.execution_status = "REQUIRES_RECONNECT"
        action.failure_code = exc.reason
        action.failure_message = None
    except Exception as exc:
        action.execution_status = "FAILED"
        action.failure_code = None
        action.failure_message = str(exc)[:500]

    action.executed_at = datetime.now(UTC)

    # Audit log del retry — sirve como registro del límite de 1 intento
    audit = DecisionAuditLog(
        id=uuid.uuid4(),
        tenant_id=action.tenant_id,
        decision_type="AGENT_ACTION_RETRIED",
        decision_data={
            "pending_action_id": str(action.id),
            "action_type": action.action_type,
            "execution_status": action.execution_status,
            "failure_code": action.failure_code,
        },
        triggered_by="agent:retry",
        actor_user_id=current_user.user_id,
        context={"execution_status_after": action.execution_status},
        created_at=datetime.now(UTC),
    )
    db.add(audit)
    await db.commit()

    logger.info(
        "pending_action_retried",
        action_id=str(pending_id),
        action_type=action.action_type,
        execution_status=action.execution_status,
        tenant_id=str(current_user.tenant_id),
    )
    response: dict[str, Any] = {
        "status": "retried",
        "action_type": action.action_type,
        "execution_status": action.execution_status,
        "failure_code": action.failure_code,
    }
    return response
