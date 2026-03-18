"""SMTP email integration."""

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.config.settings import get_settings
from app.observability.logger import get_logger

logger = get_logger(__name__)


class SMTPClient:
    def __init__(self) -> None:
        self._settings = get_settings()

    def send(
        self,
        to_email: str,
        subject: str,
        body_html: str,
        body_text: str | None = None,
    ) -> None:
        """Send an email. Logs and swallows errors to avoid blocking the caller."""
        s = self._settings

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = s.SMTP_FROM_EMAIL
        msg["To"] = to_email

        if body_text:
            msg.attach(MIMEText(body_text, "plain"))
        msg.attach(MIMEText(body_html, "html"))

        try:
            with smtplib.SMTP(s.SMTP_HOST, s.SMTP_PORT) as server:
                if s.SMTP_USE_TLS:
                    server.starttls()
                if s.SMTP_USER:
                    server.login(s.SMTP_USER, s.SMTP_PASSWORD)
                server.sendmail(s.SMTP_FROM_EMAIL, to_email, msg.as_string())
            logger.info("smtp.sent", to=to_email, subject=subject)
        except smtplib.SMTPException as exc:
            logger.error("smtp.send_failed", to=to_email, error=str(exc))
            if s.is_development:
                logger.warning(
                    "smtp.dev_fallback — SMTP failed, copy the link below to verify manually",
                    to=to_email,
                    subject=subject,
                    plain_text=body_text if body_text else "(no plain text)",
                )
