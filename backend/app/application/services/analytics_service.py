"""AnalyticsService — registro de eventos y observación cross-tenant.

Capa de orquestación sobre AnalyticsRepository. Expone:
- record_score_event(): inserta evento anonimizado tras cada recálculo de health score
- get_benchmarks_overview(): vista de administración (benchmark vigente + observación)

**El camino data-driven ya no puntúa.** La distribución observada del margen se
calcula y se muestra, pero no reemplaza al benchmark del vertical: la muestra
cuenta eventos y no negocios, así que un solo negocio recalculado cinco veces
alcanzaba el mínimo y desplazaba el benchmark de todo el rubro. Vuelve a ser
fuente de scoring cuando se pueda contar negocios distintos.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.verticals import try_parse_vertical
from app.heuristics.verticals.loader import load_margin_benchmark
from app.observability.logger import get_logger
from app.persistence.models.analytics_event import AnalyticsEvent
from app.persistence.repositories.analytics_repository import AnalyticsRepository

logger = get_logger(__name__)


class AnalyticsService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = AnalyticsRepository(session)

    async def record_score_event(
        self,
        *,
        vertical_code: str,
        score_total: int,
        score_cash: int,
        score_margin: int,
        score_stock: int,
        score_supplier: int,
        margin_ratio: float | None,
        cash_ratio: float | None,
        supplier_count: int,
        product_count: int,
        low_stock_pct: float,
        data_completeness: float,
    ) -> None:
        """Inserta evento anonimizado. Fail-silent — nunca bloquea el flujo principal.

        Usa begin_nested() (SAVEPOINT) para que un fallo aquí no contamine
        la transacción principal del health score.

        ``margin_ratio`` y ``cash_ratio`` son ``None`` cuando no se pueden
        calcular, y NO 0.0: un negocio sin ventas no tiene margen 0%, no tiene
        margen. Escribir el cero convertía cada negocio vacío en una observación
        válida que arrastraba los percentiles del rubro hacia abajo.
        """
        try:
            async with self._session.begin_nested():
                event = AnalyticsEvent(
                    vertical_code=vertical_code,
                    score_total=score_total,
                    score_cash=score_cash,
                    score_margin=score_margin,
                    score_stock=score_stock,
                    score_supplier=score_supplier,
                    margin_ratio=margin_ratio,
                    cash_ratio=cash_ratio,
                    supplier_count=supplier_count,
                    product_count=product_count,
                    low_stock_pct=low_stock_pct,
                    data_completeness=data_completeness,
                    created_at=datetime.now(UTC),
                )
                self._session.add(event)
        except Exception:
            logger.warning("analytics.record_event_failed", exc_info=True)

    async def get_benchmarks_overview(self) -> list[dict[str, object]]:
        """Resumen por vertical para ``GET /admin/analytics/benchmarks``.

        El benchmark **vigente** es siempre el estático del vertical (o el override
        del tenant, que es por-tenant y no aparece acá). La distribución observada
        se muestra al lado, como observación, y NO reemplaza a nada: ver
        ``ObservedMarginDistribution``.
        """
        vertical_codes = await self._repo.get_distinct_verticals()
        result: list[dict[str, object]] = []

        for code in vertical_codes:
            vertical = try_parse_vertical(code)
            if vertical is None:
                # Evento con un vertical fuera del catálogo (dato legado o
                # corrupto): no tiene benchmark canónico y NO se le presta el de
                # otro rubro — se omite de la vista y queda registrado.
                logger.warning("analytics.benchmarks.unknown_vertical", vertical_code=code)
                continue
            static_bm = load_margin_benchmark(vertical)
            observed = await self._repo.observed_margin_distribution(code)
            stats = await self._repo.get_vertical_stats(code)

            result.append(
                {
                    "vertical_code": code,
                    # EVENTOS de recálculo, no negocios distintos: la tabla no
                    # guarda identificador de negocio (ver ObservedMarginDistribution).
                    "event_count": stats.get("event_count", 0),
                    "avg_score": stats.get("avg_score"),
                    "avg_margin_ratio": stats.get("avg_margin"),
                    "p50_margin_ratio": stats.get("p50_margin"),
                    "avg_data_completeness": stats.get("avg_completeness"),
                    "benchmark_source": "static",
                    "benchmark": {
                        "critical_below": static_bm.critical_below,
                        "warning_below": static_bm.warning_below,
                        "healthy_min": static_bm.healthy_min,
                        "healthy_max": static_bm.healthy_max,
                    },
                    "observed_distribution": (
                        {
                            "p10": observed.p10,
                            "p25": observed.p25,
                            "p50": observed.p50,
                            "p75": observed.p75,
                            "event_count": observed.event_count,
                        }
                        if observed is not None
                        else None
                    ),
                }
            )

        return result
