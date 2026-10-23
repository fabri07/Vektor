"""Servicio de comunicación: envía mensajes a clientes/proveedores y los registra.

Resuelve el destinatario (tenant-scoped), elige el canal por nombre, intenta el
envío y persiste un `CommunicationLog` (sent/failed). Errores de configuración de
canal (`ChannelNotConfigured`) se propagan para que el router responda 503.
"""

import time
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.external_operation_service import record_external_operation
from app.integrations.communication.base import ChannelNotConfigured, MessageChannel
from app.integrations.communication.email_channel import EmailChannel
from app.integrations.communication.whatsapp_channel import WhatsAppChannel
from app.observability.logger import get_logger
from app.persistence.models.communication_log import CommunicationLog
from app.persistence.repositories.customer_repository import CustomerRepository
from app.persistence.repositories.supplier_repository import SupplierRepository

# Proveedor por canal (para el log unificado de operaciones externas).
_PROVIDER_BY_CHANNEL = {"email": "resend", "whatsapp": "whatsapp"}

logger = get_logger(__name__)

_RECIPIENT_CUSTOMER = "customer"
_RECIPIENT_SUPPLIER = "supplier"


class RecipientNotFound(Exception):  # noqa: N818
    """El destinatario no existe o no pertenece al tenant."""


class RecipientHasNoContact(Exception):  # noqa: N818
    """El destinatario no tiene el dato de contacto requerido por el canal (p. ej. email)."""


class CommunicationService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _channel(self, channel: str) -> MessageChannel:
        if channel == "email":
            return EmailChannel()
        if channel == "whatsapp":
            return WhatsAppChannel()
        raise ValueError(f"Canal no soportado: {channel!r}")

    async def _resolve_recipient_contact(
        self,
        *,
        tenant_id: UUID,
        recipient_type: str,
        recipient_id: UUID,
        channel: str,
    ) -> str:
        """Devuelve el dato de contacto (email/teléfono) del destinatario tenant-scoped."""
        if recipient_type == _RECIPIENT_CUSTOMER:
            customer = await CustomerRepository(self._session).get_by_id(recipient_id, tenant_id)
            if customer is None:
                raise RecipientNotFound("Cliente no encontrado.")
            email, phone = customer.email, customer.phone
        elif recipient_type == _RECIPIENT_SUPPLIER:
            supplier = await SupplierRepository(self._session).get_by_id(recipient_id, tenant_id)
            if supplier is None:
                raise RecipientNotFound("Proveedor no encontrado.")
            email, phone = supplier.email, supplier.phone
        else:
            raise ValueError(f"recipient_type no soportado: {recipient_type!r}")

        if channel == "email":
            if not email:
                raise RecipientHasNoContact("El destinatario no tiene email cargado.")
            return email
        if channel == "whatsapp":
            if not phone:
                raise RecipientHasNoContact("El destinatario no tiene teléfono cargado.")
            return phone
        raise ValueError(f"Canal no soportado: {channel!r}")

    async def send(
        self,
        *,
        tenant_id: UUID,
        recipient_type: str,
        recipient_id: UUID,
        channel: str,
        subject: str | None,
        body: str,
    ) -> CommunicationLog:
        """Envía el mensaje y persiste el log. Propaga ChannelNotConfigured.

        - RecipientNotFound / RecipientHasNoContact / ValueError: NO registra log
          (el envío nunca se intentó) y se propagan para que el router responda 4xx.
        - ChannelNotConfigured: se propaga (503) sin registrar log.
        - Cualquier otro error del canal: registra log status="failed" y lo devuelve.
        """
        to = await self._resolve_recipient_contact(
            tenant_id=tenant_id,
            recipient_type=recipient_type,
            recipient_id=recipient_id,
            channel=channel,
        )
        adapter = self._channel(channel)

        status = "sent"
        error: str | None = None
        provider_message_id: str | None = None
        t0 = time.monotonic()
        try:
            provider_message_id = await adapter.send(to=to, subject=subject, body=body)
        except ChannelNotConfigured:
            # Canal dormido / mal configurado: no se registra log, lo maneja el router (503).
            raise
        except Exception as exc:  # envío intentado pero falló
            status = "failed"
            error = str(exc)
            logger.error(
                "communication.send_failed",
                tenant_id=str(tenant_id),
                channel=channel,
                error=error,
                error_type=type(exc).__name__,
            )
        latency_ms = int((time.monotonic() - t0) * 1000)

        log = CommunicationLog(
            tenant_id=tenant_id,
            recipient_type=recipient_type,
            recipient_id=recipient_id,
            channel=channel,
            subject=subject,
            body=body,
            status=status,
            error=error,
            provider_message_id=provider_message_id,
        )
        self._session.add(log)
        await self._session.flush()

        # Log unificado de la operación externa (trace_id se autocaptura).
        await record_external_operation(
            self._session,
            tenant_id=tenant_id,
            operation_type=f"{channel}_send",
            provider=_PROVIDER_BY_CHANNEL.get(channel, channel),
            status="success" if status == "sent" else "failed",
            provider_request_id=provider_message_id,
            request_payload={"recipient_type": recipient_type, "to": to, "subject": subject},
            error_message=error,
            latency_ms=latency_ms,
        )
        return log

    async def history(
        self,
        *,
        tenant_id: UUID,
        recipient_type: str,
        recipient_id: UUID,
    ) -> list[CommunicationLog]:
        """Historial de comunicaciones de un destinatario (tenant-scoped, más recientes primero)."""
        from sqlalchemy import select  # noqa: PLC0415

        q = (
            select(CommunicationLog)
            .where(
                CommunicationLog.tenant_id == tenant_id,
                CommunicationLog.recipient_type == recipient_type,
                CommunicationLog.recipient_id == recipient_id,
            )
            .order_by(CommunicationLog.created_at.desc())
        )
        result = await self._session.execute(q)
        return list(result.scalars().all())
