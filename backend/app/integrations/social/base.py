"""Port (interfaz) común para los adapters de plataformas sociales.

Skeleton: el sync automático no está implementado. La carga de métricas hoy es
manual (ver `MarketingService`). Estos adapters se activarán cuando exista la
verificación OAuth de cada plataforma (fase posterior).
"""

from abc import ABC, abstractmethod
from datetime import date
from typing import Any


class SocialPlatformAdapter(ABC):
    """Interfaz de un adapter de métricas de una red social."""

    @abstractmethod
    async def fetch_metrics(
        self,
        *,
        from_date: date,
        to_date: date,
    ) -> list[dict[str, Any]]:
        """Trae métricas de la plataforma para el rango dado.

        DORMIDO: levanta NotImplementedError hasta que exista la verificación OAuth.
        """
        raise NotImplementedError

    @abstractmethod
    def available(self) -> bool:
        """True si el sync automático está habilitado y configurado."""
        raise NotImplementedError
