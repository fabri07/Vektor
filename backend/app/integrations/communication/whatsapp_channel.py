"""Canal de WhatsApp — DORMIDO (feature-flagged).

Estado: skeleton. El envío real vía Meta WhatsApp Cloud API NO está implementado:
requiere verificación de la app en Meta + número de teléfono aprobado (un trámite
externo). Mientras `ENABLE_WHATSAPP=False` (default) el canal no está disponible
y `send()` levanta `ChannelNotConfigured` → el router responde 503.

Para activarlo en una fase posterior:
  1. Verificar la app en Meta Business + alta del número (WHATSAPP_PHONE_ID).
  2. Setear ENABLE_WHATSAPP=true + WHATSAPP_TOKEN + WHATSAPP_PHONE_ID.
  3. Implementar la llamada HTTP marcada con el TODO de abajo.

`build_click_to_chat_link` es PURO (sin HTTP, sin feature flag): construye un link
wa.me que el frontend abre para que el dueño envíe desde su teléfono (v4 F6a).
"""

import re
from urllib.parse import quote

from app.config.settings import get_settings
from app.integrations.communication.base import ChannelNotConfigured, MessageChannel
from app.observability.logger import get_logger

logger = get_logger(__name__)


class WhatsAppChannel(MessageChannel):
    """Canal WhatsApp dormido — skeleton sin envío HTTP real.

    `build_click_to_chat_link` es estático y puro: no requiere instancia ni flag.
    """

    # ── Link builder — puro, sin HTTP, sin feature flag (v4 F6a) ─────────────

    @staticmethod
    def build_click_to_chat_link(to: str, body: str) -> str:
        """Construye un link wa.me/<solo-digitos>?text=<urlencoded>.

        `to` = teléfono AR; normaliza a dígitos con código de país.
        `body` se urlencodea con urllib.parse.quote. No hace HTTP.

        Supuesto de normalización (espejo razonable de frontend/src/lib/fiscal.ts):
        1. Quitar todo lo no-dígito del número.
        2. Si el resultado no empieza con "54" (código de país Argentina), se
           antepone "54". Cubre números locales como "1134567890" → "541134567890".
        """
        digits = re.sub(r"\D", "", to)
        if not digits.startswith("54"):
            digits = "54" + digits
        encoded_body = quote(body, safe="")
        return f"https://wa.me/{digits}?text={encoded_body}"

    def __init__(self) -> None:
        self._settings = get_settings()

    def available(self) -> bool:
        s = self._settings
        return bool(s.ENABLE_WHATSAPP) and bool(s.WHATSAPP_TOKEN)

    async def send(self, *, to: str, subject: str | None, body: str) -> str | None:
        if not self.available():
            raise ChannelNotConfigured(
                "WhatsApp no configurado (requiere verificación de Meta — Fase posterior)"
            )

        # TODO (Fase posterior): envío real vía Meta WhatsApp Cloud API.
        #   POST https://graph.facebook.com/v21.0/{WHATSAPP_PHONE_ID}/messages
        #   headers={"Authorization": f"Bearer {self._settings.WHATSAPP_TOKEN}"}
        #   json={
        #       "messaging_product": "whatsapp",
        #       "to": to,
        #       "type": "text",
        #       "text": {"body": body},
        #   }
        # `subject` se ignora: WhatsApp no tiene asunto. Por ahora NO se hace la
        # llamada HTTP real aunque el flag esté activo — se documenta y se loguea.
        logger.warning(
            "whatsapp.send_not_implemented — flag activo pero envío real pendiente",
            to=to,
        )
        raise ChannelNotConfigured(
            "WhatsApp habilitado pero el envío real aún no está implementado (Fase posterior)"
        )
