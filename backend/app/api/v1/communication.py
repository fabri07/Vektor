"""Endpoints de comunicación: enviar mensajes a clientes/proveedores + historial.

Email funcional vía Resend; WhatsApp dormido (feature-flagged → 503).
"""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_tenant, require_role
from app.application.services.communication_service import (
    CommunicationService,
    RecipientHasNoContact,
    RecipientNotFound,
)
from app.integrations.communication.base import ChannelNotConfigured
from app.integrations.communication.email_channel import EmailChannel
from app.integrations.communication.whatsapp_channel import WhatsAppChannel
from app.persistence.db.session import get_db_session
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.communication_log import CommunicationLog
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.schemas.communication import (
    ChannelStatus,
    CommunicationLogResponse,
    SendMessageRequest,
)

router = APIRouter()


def _audit_send(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    user_id: UUID,
    log: CommunicationLog,
) -> None:
    session.add(
        DecisionAuditLog(
            tenant_id=tenant_id,
            decision_type="COMMUNICATION_SENT",
            decision_data={
                "communication_log_id": str(log.id),
                "recipient_type": log.recipient_type,
                "recipient_id": str(log.recipient_id),
                "channel": log.channel,
                "status": log.status,
                "source": "ui",
            },
            triggered_by="ui:communication",
            actor_user_id=user_id,
            context={"endpoint": "communication"},
            created_at=datetime.now(UTC),
        )
    )


async def _send(
    *,
    session: AsyncSession,
    tenant_id: UUID,
    user_id: UUID,
    recipient_type: str,
    recipient_id: UUID,
    body: SendMessageRequest,
) -> CommunicationLog:
    service = CommunicationService(session)
    try:
        log = await service.send(
            tenant_id=tenant_id,
            recipient_type=recipient_type,
            recipient_id=recipient_id,
            channel=body.channel,
            subject=body.subject,
            body=body.body,
        )
    except RecipientNotFound as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RecipientHasNoContact as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ChannelNotConfigured as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "CHANNEL_NOT_CONFIGURED", "message": str(exc)},
        ) from exc
    _audit_send(session, tenant_id=tenant_id, user_id=user_id, log=log)
    return log


@router.get(
    "/channels",
    response_model=list[ChannelStatus],
    summary="Estado de los canales de comunicación",
)
async def list_channels(
    tenant: Tenant = Depends(get_current_tenant),
) -> list[ChannelStatus]:
    return [
        ChannelStatus(channel="email", available=EmailChannel().available()),
        ChannelStatus(channel="whatsapp", available=WhatsAppChannel().available()),
    ]


@router.post(
    "/customers/{customer_id}/send",
    response_model=CommunicationLogResponse,
    summary="Enviar mensaje a un cliente",
)
async def send_to_customer(
    customer_id: UUID,
    body: SendMessageRequest,
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(require_role("OWNER", "ADMIN")),
    session: AsyncSession = Depends(get_db_session),
) -> CommunicationLog:
    return await _send(
        session=session,
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        recipient_type="customer",
        recipient_id=customer_id,
        body=body,
    )


@router.get(
    "/customers/{customer_id}/history",
    response_model=list[CommunicationLogResponse],
    summary="Historial de comunicaciones de un cliente",
)
async def customer_history(
    customer_id: UUID,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> list[CommunicationLog]:
    service = CommunicationService(session)
    return await service.history(
        tenant_id=tenant.tenant_id,
        recipient_type="customer",
        recipient_id=customer_id,
    )


@router.post(
    "/suppliers/{supplier_id}/send",
    response_model=CommunicationLogResponse,
    summary="Enviar mensaje a un proveedor",
)
async def send_to_supplier(
    supplier_id: UUID,
    body: SendMessageRequest,
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(require_role("OWNER", "ADMIN")),
    session: AsyncSession = Depends(get_db_session),
) -> CommunicationLog:
    return await _send(
        session=session,
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        recipient_type="supplier",
        recipient_id=supplier_id,
        body=body,
    )


@router.get(
    "/suppliers/{supplier_id}/history",
    response_model=list[CommunicationLogResponse],
    summary="Historial de comunicaciones de un proveedor",
)
async def supplier_history(
    supplier_id: UUID,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> list[CommunicationLog]:
    service = CommunicationService(session)
    return await service.history(
        tenant_id=tenant.tenant_id,
        recipient_type="supplier",
        recipient_id=supplier_id,
    )
