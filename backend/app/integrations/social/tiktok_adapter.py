"""Adapter TikTok — DORMIDO (skeleton).

El sync vía TikTok Business API requiere verificación de la app (OAuth) +
permisos de analytics. Mientras `ENABLE_SOCIAL_SYNC=False` (default) este adapter
no está disponible y `fetch_metrics` levanta NotImplementedError. La carga de
métricas hoy es manual.
"""

from datetime import date
from typing import Any

from app.config.settings import get_settings
from app.integrations.social.base import SocialPlatformAdapter


class TikTokAdapter(SocialPlatformAdapter):
    """Skeleton del adapter TikTok."""

    def __init__(self) -> None:
        self._settings = get_settings()

    def available(self) -> bool:
        return bool(self._settings.ENABLE_SOCIAL_SYNC)

    async def fetch_metrics(
        self,
        *,
        from_date: date,
        to_date: date,
    ) -> list[dict[str, Any]]:
        # TODO (Fase posterior): TikTok Business API (Follower/Video Insights +
        # ads spend). Requiere TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET + tokens
        # OAuth por tenant.
        raise NotImplementedError(
            "Requiere verificación de app (OAuth) — fase posterior"
        )
