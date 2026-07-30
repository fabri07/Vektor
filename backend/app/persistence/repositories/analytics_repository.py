"""Analytics repository — inserts y consultas cross-tenant sobre analytics_events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.analytics_event import AnalyticsEvent

_MIN_EVENTS = 5
_LOOKBACK_DAYS = 90

#: Versión mínima del contrato de escritura para que un evento sea
#: estadísticamente usable. Las filas v1 guardan ``margin_ratio = 0.0`` para todo
#: negocio sin ventas, y ese cero fabricado es indistinguible de un cero genuino
#: una vez en la tabla: se descartan enteras.
#:
#: Es una constante SEPARADA de ``EVENT_SCHEMA_VERSION`` aunque hoy valgan lo
#: mismo. Lo que se escribe y lo que se considera confiable son dos preguntas
#: distintas: un v3 futuro que agregue un campo no invalida a los v2, y atarlas a
#: la misma constante haría que ese bump descartara en silencio dos años de
#: observación válida.
_MIN_TRUSTED_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class ObservedMarginDistribution:
    """Distribución observada del margen de un vertical. **No es un benchmark.**

    Es deliberadamente un tipo distinto de ``MarginBenchmark`` para que no se
    pueda pasar uno donde se espera el otro. Un benchmark es NORMATIVO ("cuánto
    debería ganar este rubro"); esto es DESCRIPTIVO ("cuánto gana hoy la muestra
    que tenemos"). Mapear p50 a "piso sano" —como se hacía— deja por construcción
    a la mitad de los negocios debajo del piso, y si el rubro entero se funde el
    umbral se funde con él y nadie ve la alerta.

    ``event_count`` cuenta EVENTOS DE RECÁLCULO, no negocios distintos:
    ``analytics_events`` no guarda ``tenant_id`` ni un seudónimo estable, así que
    un mismo negocio recalculado cinco veces cuenta cinco. Por eso esta
    distribución alimenta solo la vista de administración y no el scoring.
    """

    p10: float
    p25: float
    p50: float
    p75: float
    event_count: int


class AnalyticsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def observed_margin_distribution(
        self,
        vertical_code: str,
        min_events: int = _MIN_EVENTS,
        lookback_days: int = _LOOKBACK_DAYS,
    ) -> ObservedMarginDistribution | None:
        """Percentiles del margen observado, o None si no hay eventos suficientes.

        Solo para observación (``GET /admin/analytics/benchmarks``). No vuelve al
        scoring hasta que se pueda contar negocios distintos — ver el docstring de
        ``ObservedMarginDistribution``.
        """
        cutoff = datetime.now(UTC) - timedelta(days=lookback_days)

        count_q = (
            select(func.count())
            .select_from(AnalyticsEvent)
            .where(
                AnalyticsEvent.vertical_code == vertical_code,
                AnalyticsEvent.created_at >= cutoff,
                AnalyticsEvent.schema_version >= _MIN_TRUSTED_SCHEMA_VERSION,
                AnalyticsEvent.margin_ratio.is_not(None),
            )
        )
        count = (await self._session.scalar(count_q)) or 0
        if count < min_events:
            return None

        q = select(
            func.percentile_cont(0.10).within_group(AnalyticsEvent.margin_ratio).label("p10"),
            func.percentile_cont(0.25).within_group(AnalyticsEvent.margin_ratio).label("p25"),
            func.percentile_cont(0.50).within_group(AnalyticsEvent.margin_ratio).label("p50"),
            func.percentile_cont(0.75).within_group(AnalyticsEvent.margin_ratio).label("p75"),
        ).where(
            AnalyticsEvent.vertical_code == vertical_code,
            AnalyticsEvent.created_at >= cutoff,
            AnalyticsEvent.schema_version >= _MIN_TRUSTED_SCHEMA_VERSION,
            AnalyticsEvent.margin_ratio.is_not(None),
        )
        row = (await self._session.execute(q)).one_or_none()
        if row is None or row.p10 is None:
            return None

        return ObservedMarginDistribution(
            p10=float(row.p10),
            p25=float(row.p25),
            p50=float(row.p50),
            p75=float(row.p75),
            event_count=count,
        )

    async def get_vertical_stats(
        self, vertical_code: str, lookback_days: int = _LOOKBACK_DAYS
    ) -> dict[str, object]:
        """Estadísticas agregadas de un vertical (eventos, avg score, p50 margin).

        ``event_count`` son eventos de recálculo, NO negocios distintos: la tabla
        no guarda identificador de negocio. Mismo corte de confianza que
        ``observed_margin_distribution`` para que la vista no mezcle eventos
        pre-fix (con ceros fabricados) con los posteriores.
        """
        cutoff = datetime.now(UTC) - timedelta(days=lookback_days)

        count_q = (
            select(func.count())
            .select_from(AnalyticsEvent)
            .where(
                AnalyticsEvent.vertical_code == vertical_code,
                AnalyticsEvent.created_at >= cutoff,
                AnalyticsEvent.schema_version >= _MIN_TRUSTED_SCHEMA_VERSION,
            )
        )
        count = (await self._session.scalar(count_q)) or 0
        if count == 0:
            return {"event_count": 0}

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
            AnalyticsEvent.schema_version >= _MIN_TRUSTED_SCHEMA_VERSION,
        )
        row = (await self._session.execute(q)).one_or_none()
        if row is None:
            return {"event_count": count}

        return {
            "event_count": count,
            "avg_score": round(float(row.avg_score), 1) if row.avg_score is not None else None,
            "avg_margin": round(float(row.avg_margin), 4) if row.avg_margin is not None else None,
            "p50_margin": round(float(row.p50_margin), 4) if row.p50_margin is not None else None,
            "avg_completeness": (
                round(float(row.avg_completeness), 3) if row.avg_completeness is not None else None
            ),
        }

    async def get_distinct_verticals(self, lookback_days: int = _LOOKBACK_DAYS) -> list[str]:
        """Vertical codes con datos dentro de la ventana.

        Mismo corte de confianza que los otros dos métodos: si no lo aplicara,
        la vista de administración listaría verticales cuyos únicos eventos son
        anteriores al fix, con `event_count: 0` y sin distribución — una fila que
        parece un rubro sin actividad cuando en realidad es un rubro cuya
        actividad se descartó.
        """
        cutoff = datetime.now(UTC) - timedelta(days=lookback_days)
        q = (
            select(AnalyticsEvent.vertical_code)
            .distinct()
            .where(
                AnalyticsEvent.created_at >= cutoff,
                AnalyticsEvent.schema_version >= _MIN_TRUSTED_SCHEMA_VERSION,
            )
        )
        rows = (await self._session.execute(q)).scalars().all()
        return sorted(rows)
