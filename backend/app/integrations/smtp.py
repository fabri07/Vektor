"""Email integration via Resend HTTP API."""

from typing import Any

import httpx

from app.config.settings import get_settings
from app.observability.logger import get_logger

logger = get_logger(__name__)

_RESEND_API_URL = "https://api.resend.com/emails"

#: 4xx que SÍ pueden resolverse reintentando: timeout del pedido y límite de tasa.
_4XX_REINTENTABLES = frozenset({408, 429})


def es_rechazo_permanente(exc: BaseException) -> bool:
    """El proveedor rechazó ESTE mail por su contenido o destinatario (4xx).

    Reintentar el mismo pedido no lo arregla (p. ej. un 422 por un destinatario
    inválido): hay que sellarlo como fallido ya, no gastar los reintentos ni
    demorar el aviso. Los 5xx, los cortes de red y los 408/429 son transitorios
    y siguen reintentándose.
    """
    if not isinstance(exc, httpx.HTTPStatusError):
        return False
    codigo = exc.response.status_code
    return 400 <= codigo < 500 and codigo not in _4XX_REINTENTABLES


def remitente(from_email: str, from_name: str) -> str:
    """`From` del mail: con nombre visible, sin duplicarlo si ya viene armado."""
    base = from_email.strip()
    nombre = from_name.strip()
    if not nombre or "<" in base:
        return base
    return f"{nombre} <{base}>"


class SMTPClient:
    def __init__(self) -> None:
        self._settings = get_settings()

    def _api_key(self) -> str:
        s = self._settings
        return s.RESEND_API_KEY or s.SMTP_PASSWORD

    def send(
        self,
        to_email: str,
        subject: str,
        body_html: str,
        body_text: str | None = None,
        *,
        raise_on_error: bool = False,
    ) -> str | None:
        """Send an email via Resend HTTP API.

        Returns the Resend message id on success, or None (dev/no-key fallback).
        On a real send failure: re-raises if `raise_on_error=True` (para que el
        canal de comunicación lo registre); de lo contrario loguea y devuelve None
        (comportamiento histórico para auth/reportes que no deben romper).
        """
        s = self._settings
        api_key = self._api_key()

        if not api_key:
            logger.warning("smtp.skipped — no Resend API key configured", to=to_email)
            if s.is_development:
                logger.warning(
                    "smtp.dev_fallback — copia el link para verificar manualmente",
                    to=to_email,
                    subject=subject,
                    plain_text=body_text or "(sin texto plano)",
                )
            return None

        payload: dict[str, Any] = {
            "from": remitente(s.SMTP_FROM_EMAIL, s.EMAIL_FROM_NAME),
            "to": [to_email],
            "subject": subject,
            "html": body_html,
        }
        reply_to = s.EMAIL_REPLY_TO.strip()
        if reply_to:
            payload["reply_to"] = reply_to
        if body_text:
            payload["text"] = body_text

        try:
            with httpx.Client(timeout=15.0) as client:
                response = client.post(
                    _RESEND_API_URL,
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=payload,
                )
                response.raise_for_status()
                try:
                    message_id: str | None = response.json().get("id")
                except Exception:  # respuesta sin JSON / sin id — no es fatal
                    message_id = None
            logger.info("smtp.sent", to=to_email, subject=subject, provider_message_id=message_id)
            return message_id
        except httpx.HTTPStatusError as exc:
            logger.error(
                "smtp.send_failed",
                to=to_email,
                status_code=exc.response.status_code,
                response_body=exc.response.text,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            if raise_on_error:
                raise
            return None
        except Exception as exc:
            logger.error(
                "smtp.send_failed",
                to=to_email,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            if raise_on_error:
                raise
            return None
