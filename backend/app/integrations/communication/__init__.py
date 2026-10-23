"""Canales de comunicación con clientes/proveedores (port/adapter por canal).

Fase 5: email real vía Resend (funcional) + WhatsApp dormido (feature-flagged).
"""

from app.integrations.communication.base import ChannelNotConfigured, MessageChannel
from app.integrations.communication.email_channel import EmailChannel
from app.integrations.communication.whatsapp_channel import WhatsAppChannel

__all__ = [
    "ChannelNotConfigured",
    "MessageChannel",
    "EmailChannel",
    "WhatsAppChannel",
]
