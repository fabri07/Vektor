"""Port (interfaz) común para los canales de comunicación.

Cada canal (email, WhatsApp, …) implementa `MessageChannel`. El servicio de
comunicación elige el adapter por `channel` y delega el envío. Mantener esta
interfaz mínima permite sumar canales nuevos sin tocar el servicio ni el router.
"""

from abc import ABC, abstractmethod


class ChannelNotConfigured(Exception):
    """El canal solicitado no está disponible/configurado.

    El router la traduce a 503 `{"code": "CHANNEL_NOT_CONFIGURED"}`. La levantan
    los canales dormidos (p. ej. WhatsApp sin verificación de Meta).
    """


class MessageChannel(ABC):
    """Interfaz de un canal de comunicación saliente."""

    @abstractmethod
    async def send(self, *, to: str, subject: str | None, body: str) -> None:
        """Envía un mensaje al destinatario `to`.

        `subject` puede ser None para canales que no lo soportan (WhatsApp).
        Levanta `ChannelNotConfigured` si el canal no está disponible.
        """
        raise NotImplementedError

    @abstractmethod
    def available(self) -> bool:
        """True si el canal está configurado y puede enviar."""
        raise NotImplementedError
