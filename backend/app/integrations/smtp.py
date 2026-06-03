"""Email integration via Resend HTTP API."""

from typing import Any

import httpx

from app.config.settings import get_settings
from app.observability.logger import get_logger

logger = get_logger(__name__)

_RESEND_API_URL = "https://api.resend.com/emails"


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
    ) -> None:
        """Send an email via Resend HTTP API."""
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
            return

        payload: dict[str, Any] = {
            "from": s.SMTP_FROM_EMAIL,
            "to": [to_email],
            "subject": subject,
            "html": body_html,
        }
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
            logger.info("smtp.sent", to=to_email, subject=subject)
        except httpx.HTTPStatusError as exc:
            logger.error(
                "smtp.send_failed",
                to=to_email,
                status_code=exc.response.status_code,
                response_body=exc.response.text,
                error=str(exc),
                error_type=type(exc).__name__,
            )
        except Exception as exc:
            logger.error(
                "smtp.send_failed",
                to=to_email,
                error=str(exc),
                error_type=type(exc).__name__,
            )
