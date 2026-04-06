"""Endpoints del sistema multiagente de Véktor.

POST /api/v1/agent/chat      — procesa mensaje, crea pending_action si MEDIUM/HIGH
POST /api/v1/agent/confirm/{pending_id} — confirma y ejecuta una acción pendiente
POST /api/v1/agent/cancel/{pending_id}  — rechaza una acción pendiente
"""

import uuid
from datetime import UTC, date, datetime, time
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user
from app.integrations.google_workspace.exceptions import WorkspaceTokenError
from app.application.agents.base import BaseAgent
from app.application.agents.ceo.agent import AgentCEO
from app.application.agents.shared.schemas import AgentRequest, AgentResponse
from app.application.services.pending_action_service import (
    cancel_pending_action,
    create_pending_action,
    execute_pending_action,
)
from app.observability.logger import get_logger
from app.persistence.db.redis_client import get_redis
from app.persistence.db.session import get_db_session
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.pending_action import PendingAction
from app.persistence.models.user import User

router = APIRouter()
logger = get_logger(__name__)


def _get_sub_agent(
    name: str,
    db: Optional[AsyncSession] = None,
    redis: Optional[Redis] = None,
    user_id: Optional[uuid.UUID] = None,
    tenant_id: Optional[uuid.UUID] = None,
) -> Optional[BaseAgent]:
    """Devuelve el subagente correspondiente al nombre; None si no hay mapeo."""
    if name == "agent_cash":
        from app.application.agents.cash.agent import AgentCash  # noqa: PLC0415
        return AgentCash(db=db, redis=redis)
    if name == "agent_stock":
        from app.application.agents.stock.agent import AgentStock  # noqa: PLC0415
        return AgentStock()
    if name == "agent_supplier":
        from app.application.agents.supplier.agent import AgentSupplier  # noqa: PLC0415
        gateway = None
        if db is not None and redis is not None and user_id is not None and tenant_id is not None:
            from app.integrations.google_workspace.gateway import GoogleWorkspaceGateway  # noqa: PLC0415
            gateway = GoogleWorkspaceGateway(db, redis, user_id, tenant_id)
        return AgentSupplier(session=db, gateway=gateway)
    if name == "agent_health":
        from app.application.agents.health.agent import AgentHealth  # noqa: PLC0415
        return AgentHealth(db=db)
    if name == "agent_helper":
        from app.application.agents.helper.agent import AgentHelper  # noqa: PLC0415
        return AgentHelper()
    return None


class ChatRequest(BaseModel):
    message: str
    attachments: list[Any] = []
    conversation_id: Optional[str] = None


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
    rate_key = f"rate:chat:{tenant_id}:{date.today()}"
    count = await redis.incr(rate_key)
    if count == 1:
        # Primer request del día: configurar expiración a medianoche
        midnight = datetime.combine(date.today(), time.max).replace(tzinfo=UTC)
        await redis.expireat(rate_key, midnight)
    if count > 50:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Límite diario de 50 mensajes alcanzado. Disponible mañana.",
        )

    # ── AgentCEO: clasificar intent y evaluar riesgo ──────────────────────────
    ceo = AgentCEO()
    request = AgentRequest(
        user_id=str(user_id),
        business_id=str(tenant_id),
        message=body.message,
        attachments=body.attachments,
        conversation_id=body.conversation_id,
    )
    try:
        agent_response = await ceo.process(request)
    except Exception as exc:
        logger.error("agent_ceo_process_failed", error=str(exc), error_type=type(exc).__name__, tenant_id=str(tenant_id))
        raise HTTPException(status_code=500, detail=f"Agent error: {type(exc).__name__}: {exc}") from exc

    # ── Despacho al subagente que corresponde al intent ───────────────────────
    target_agent_name: str = agent_response.result.get("target_agent", "")
    sub_agent = _get_sub_agent(
        target_agent_name,
        db=db,
        redis=redis,
        user_id=current_user.user_id,
        tenant_id=current_user.tenant_id,
    )
    if sub_agent is not None:
        try:
            agent_response = await sub_agent.process(request)
        except Exception as exc:
            logger.error("sub_agent_process_failed", agent=target_agent_name, error=str(exc), error_type=type(exc).__name__, tenant_id=str(tenant_id))
            raise HTTPException(status_code=500, detail=f"Sub-agent error ({target_agent_name}): {type(exc).__name__}: {exc}") from exc

    # ── Si requiere aprobación: crear pending_action ──────────────────────────
    if agent_response.requires_approval:
        action_type = agent_response.result.get("action_type", "ANSWER_HELP_REQUEST")
        pending = await create_pending_action(
            db=db,
            tenant_id=tenant_id,
            user_id=user_id,
            action_type=action_type,
            payload=agent_response.result.get("structured_data", agent_response.result.get("entities", {})),
            risk_level=str(agent_response.risk_level),
        )
        agent_response.pending_action_id = str(pending.id)

    # ── Audit log (insert-only) ───────────────────────────────────────────────
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
            "pending_action_id": agent_response.pending_action_id,
        },
        triggered_by="agent:chat",
        actor_user_id=user_id,
        context={"message_length": len(body.message)},
        created_at=datetime.now(UTC),
    )
    db.add(audit)
    await db.commit()

    return agent_response


# ── POST /confirm/{pending_id} ────────────────────────────────────────────────


@router.post("/confirm/{pending_id}", summary="Confirmar acción pendiente")
async def confirm_action(
    pending_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
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
        # Para acciones externas: lifecycle completo IN_PROGRESS → SUCCEEDED|FAILED|REQUIRES_RECONNECT.
        # Nunca se re-lanza la excepción — el fallo se persiste y el endpoint responde con estado.
        action.execution_status = "IN_PROGRESS"
        await db.flush()  # visible antes de la llamada externa
        try:
            await execute_pending_action(action, db, redis=redis)
            action.execution_status = "SUCCEEDED"
        except WorkspaceTokenError as exc:
            action.execution_status = "REQUIRES_RECONNECT"
            action.failure_code = exc.reason
            action.failure_message = None
        except Exception as exc:
            action.execution_status = "FAILED"
            action.failure_code = None
            action.failure_message = str(exc)[:500]
    else:
        # Para acciones locales: ejecución en la misma transacción; excepción se propaga (fail-closed).
        await execute_pending_action(action, db, redis=redis)
        action.execution_status = "SUCCEEDED"

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
    }
    if action.execution_status == "REQUIRES_RECONNECT":
        response["reconnect_required"] = True
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


# ── POST /retry/{pending_id} ──────────────────────────────────────────────────


@router.post("/retry/{pending_id}", summary="Reintentar acción fallida")
async def retry_action(
    pending_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
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
            detail=f"La acción '{action.action_type}' no es reintentable (acción local sin sistema externo).",
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
    except WorkspaceTokenError as exc:
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
    }
    if action.execution_status == "REQUIRES_RECONNECT":
        response["reconnect_required"] = True
    return response
