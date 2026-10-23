"""Adapter Meta (Instagram + Facebook) — DORMIDO (skeleton).

El sync vía Meta Graph API requiere verificación de la app (OAuth) + permisos
(instagram_basic, pages_read_engagement, ads_read). Mientras
`ENABLE_SOCIAL_SYNC=False` (default) este adapter no está disponible y
`fetch_metrics` levanta NotImplementedError. La carga de métricas hoy es manual.
"""

from datetime import date
from typing import Any

from app.config.settings import get_settings
from app.integrations.social.base import SocialPlatformAdapter


class MetaAdapter(SocialPlatformAdapter):
    """Skeleton del adapter Meta (Instagram/Facebook)."""

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
        # TODO (Fase posterior): Meta Graph API (Instagram Insights + Page Insights
        # + Marketing API para ads_spend). Requiere META_APP_ID / META_APP_SECRET +
        # tokens OAuth por tenant.
        raise NotImplementedError(
            "Requiere verificación de app (OAuth) — fase posterior"
        )
