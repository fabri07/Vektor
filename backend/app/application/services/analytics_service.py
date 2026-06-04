"""AnalyticsService — registro de eventos y cómputo de benchmarks cross-tenant.

Capa de orquestación sobre AnalyticsRepository. Expone:
- record_score_event(): inserta evento anonimizado tras cada recálculo de health score
- get_data_driven_benchmark(): devuelve MarginBenchmark estadístico si hay suficientes datos
- get_benchmarks_overview(): vista completa para la API interna de Véktor
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.heuristics.verticals import MarginBenchmark
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
        margin_ratio: float,
        cash_ratio: float,
        supplier_count: int,
        product_count: int,
        low_stock_pct: float,
        data_completeness: float,
    ) -> None:
        """Inserta evento anonimizado. Fail-silent — nunca bloquea el flujo principal.

        Usa begin_nested() (SAVEPOINT) para que un fallo aquí no contamine
        la transacción principal del health score.
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

    async def get_data_driven_benchmark(self, vertical_code: str) -> MarginBenchmark | None:
        """Devuelve benchmark estadístico si hay >= 5 muestras. None = usar estático."""
        return await self._repo.compute_margin_benchmark(vertical_code)

    async def get_benchmarks_overview(self) -> list[dict[str, object]]:
        """Resumen de benchmarks para todos los verticales con datos.

        Usado por GET /admin/analytics/benchmarks.
        """
        vertical_codes = await self._repo.get_distinct_verticals()
        result: list[dict[str, object]] = []

        for code in vertical_codes:
            data_bm = await self._repo.compute_margin_benchmark(code)
            static_bm = load_margin_benchmark(code)
            stats = await self._repo.get_vertical_stats(code)
            active_bm = data_bm if data_bm is not None else static_bm

            result.append(
                {
                    "vertical_code": code,
                    "sample_count": stats.get("sample_count", 0),
                    "avg_score": stats.get("avg_score"),
                    "avg_margin_ratio": stats.get("avg_margin"),
                    "p50_margin_ratio": stats.get("p50_margin"),
                    "avg_data_completeness": stats.get("avg_completeness"),
                    "benchmark_source": "data_driven" if data_bm is not None else "static",
                    "benchmark": {
                        "critical_below": active_bm.critical_below,
                        "warning_below": active_bm.warning_below,
                        "healthy_min": active_bm.healthy_min,
                        "healthy_max": active_bm.healthy_max,
                    },
                }
            )

        return result
