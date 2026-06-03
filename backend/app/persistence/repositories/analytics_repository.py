"""Analytics repository — inserts y consultas cross-tenant sobre analytics_events."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.heuristics.verticals import MarginBenchmark
from app.persistence.models.analytics_event import AnalyticsEvent

_MIN_SAMPLES = 5
_LOOKBACK_DAYS = 90


class AnalyticsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def compute_margin_benchmark(
        self,
        vertical_code: str,
        min_samples: int = _MIN_SAMPLES,
        lookback_days: int = _LOOKBACK_DAYS,
    ) -> MarginBenchmark | None:
        """Computa MarginBenchmark con percentiles reales.

        Devuelve None si no hay suficientes muestras (usa fallback estático).
        Los percentiles p10/p25/p50/p75 del margin_ratio histórico definen
        critical_below / warning_below / healthy_min / healthy_max.
        """
        cutoff = datetime.now(UTC) - timedelta(days=lookback_days)

        count_q = (
            select(func.count())
            .select_from(AnalyticsEvent)
            .where(
                AnalyticsEvent.vertical_code == vertical_code,
                AnalyticsEvent.created_at >= cutoff,
                AnalyticsEvent.margin_ratio.is_not(None),
            )
        )
        count = (await self._session.scalar(count_q)) or 0
        if count < min_samples:
            return None

        q = select(
            func.percentile_cont(0.10).within_group(AnalyticsEvent.margin_ratio).label("p10"),
            func.percentile_cont(0.25).within_group(AnalyticsEvent.margin_ratio).label("p25"),
            func.percentile_cont(0.50).within_group(AnalyticsEvent.margin_ratio).label("p50"),
            func.percentile_cont(0.75).within_group(AnalyticsEvent.margin_ratio).label("p75"),
        ).where(
            AnalyticsEvent.vertical_code == vertical_code,
            AnalyticsEvent.created_at >= cutoff,
            AnalyticsEvent.margin_ratio.is_not(None),
        )
        row = (await self._session.execute(q)).one_or_none()
        if row is None or row.p10 is None:
            return None

        return MarginBenchmark(
            critical_below=float(row.p10),
            warning_below=float(row.p25),
            healthy_min=float(row.p50),
            healthy_max=float(row.p75),
        )

    async def get_vertical_stats(
        self, vertical_code: str, lookback_days: int = _LOOKBACK_DAYS
    ) -> dict[str, object]:
        """Estadísticas agregadas de un vertical (count, avg score, p50 margin)."""
        cutoff = datetime.now(UTC) - timedelta(days=lookback_days)

        count_q = (
            select(func.count())
            .select_from(AnalyticsEvent)
            .where(
                AnalyticsEvent.vertical_code == vertical_code,
                AnalyticsEvent.created_at >= cutoff,
            )
        )
        count = (await self._session.scalar(count_q)) or 0
        if count == 0:
            return {"sample_count": 0}

        q = select(
            func.avg(AnalyticsEvent.score_total).label("avg_score"),
            func.avg(AnalyticsEvent.margin_ratio).label("avg_margin"),
            func.percentile_cont(0.50)
            .within_group(AnalyticsEvent.margin_ratio)
            .label("p50_margin"),
            func.avg(AnalyticsEvent.data_completeness).label("avg_completeness"),
        ).where(
            AnalyticsEvent.vertical_code == vertical_code,
            AnalyticsEvent.created_at >= cutoff,
        )
        row = (await self._session.execute(q)).one_or_none()
        if row is None:
            return {"sample_count": count}

        return {
            "sample_count": count,
            "avg_score": round(float(row.avg_score), 1) if row.avg_score is not None else None,
            "avg_margin": round(float(row.avg_margin), 4) if row.avg_margin is not None else None,
            "p50_margin": round(float(row.p50_margin), 4) if row.p50_margin is not None else None,
            "avg_completeness": (
                round(float(row.avg_completeness), 3) if row.avg_completeness is not None else None
            ),
        }

    async def get_distinct_verticals(self, lookback_days: int = _LOOKBACK_DAYS) -> list[str]:
        """Vertical codes con datos dentro del lookback window."""
        cutoff = datetime.now(UTC) - timedelta(days=lookback_days)
        q = (
            select(AnalyticsEvent.vertical_code)
            .distinct()
            .where(AnalyticsEvent.created_at >= cutoff)
        )
        rows = (await self._session.execute(q)).scalars().all()
        return sorted(rows)
