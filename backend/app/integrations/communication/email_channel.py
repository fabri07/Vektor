"""Canal de email — adapter sobre el cliente Resend existente (`SMTPClient`).

`SMTPClient.send` es síncrono (httpx.Client). El envío es de baja latencia y se
hace dentro del request; se envuelve el resultado en la interfaz async del canal.
"""

from app.integrations.communication.base import MessageChannel
from app.integrations.smtp import SMTPClient


class EmailChannel(MessageChannel):
    """Envía emails vía Resend reutilizando `SMTPClient`."""

    def __init__(self, smtp_client: SMTPClient | None = None) -> None:
        self._smtp = smtp_client or SMTPClient()

    def available(self) -> bool:
        # Resend siempre está disponible como canal: en dev sin API key el
        # SMTPClient loguea y no rompe (no falla el envío).
        return True

    async def send(self, *, to: str, subject: str | None, body: str) -> None:
        self._smtp.send(
            to_email=to,
            subject=subject or "",
            body_html=body,
            body_text=body,
        )
