"""Adapters de plataformas sociales (skeleton DORMIDO — Fase 4).

La carga de métricas es 100% manual por ahora. Estos adapters quedan como
skeleton para el sync automático vía OAuth de una fase posterior (requiere
verificación de la app en Meta/TikTok). `available()` → False mientras
`ENABLE_SOCIAL_SYNC=False` (default).
"""

from app.integrations.social.base import SocialPlatformAdapter
from app.integrations.social.meta_adapter import MetaAdapter
from app.integrations.social.tiktok_adapter import TikTokAdapter

__all__ = [
    "SocialPlatformAdapter",
    "MetaAdapter",
    "TikTokAdapter",
]
