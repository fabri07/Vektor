"""Servicio de dashboard de marketing — agregación determinística (sin LLM).

Une las métricas de redes cargadas manualmente + el cruce ads vs ventas. Respeta
la regla no-invention: si no hay métricas en el período, devuelve una estructura
vacía clara (`has_data=False`), sin inventar valores.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.repositories.social_metric_repository import SocialMetricRepository
from app.persistence.repositories.transaction_repository import SaleRepository
from app.schemas.marketing import (
    AdsVsSales,
    MarketingDashboardResponse,
    PlatformDashboard,
)


@dataclass
class _PlatformAcc:
    """Acumulador determinístico por plataforma durante la agregación."""

    followers: int = 0
    followers_date: date | None = None
    reach: int = 0
    engagement: int = 0
    ads_spend_ars: Decimal = field(default_factory=lambda: Decimal("0"))


class MarketingService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_dashboard(self, tenant_id: UUID, days: int = 30) -> MarketingDashboardResponse:
        to_date = date.today()
        from_date = to_date - timedelta(days=days)

        repo = SocialMetricRepository(self._session)
        metrics = await repo.list_by_tenant(
            tenant_id, from_date=from_date, to_date=to_date
        )

        # ── Agregación por plataforma ──────────────────────────────────────────
        # followers = último valor conocido (métrica más reciente); reach/engagement/
        # ads = suma del período. `metrics` viene ordenado por metric_date desc.
        _cents = Decimal("0.01")
        by_platform: dict[str, _PlatformAcc] = {}
        for m in metrics:
            slot = by_platform.setdefault(m.platform, _PlatformAcc())
            slot.reach += m.reach
            slot.engagement += m.engagement
            slot.ads_spend_ars += m.ads_spend_ars
            # followers: tomar el de la fecha más reciente.
            if slot.followers_date is None or m.metric_date > slot.followers_date:
                slot.followers = m.followers
                slot.followers_date = m.metric_date

        platforms = [
            PlatformDashboard(
                platform=platform,
                followers=slot.followers,
                reach=slot.reach,
                engagement=slot.engagement,
                ads_spend_ars=slot.ads_spend_ars.quantize(_cents),
            )
            for platform, slot in sorted(by_platform.items())
        ]

        total_followers = sum(p.followers for p in platforms)
        total_reach = sum(p.reach for p in platforms)
        total_engagement = sum(p.engagement for p in platforms)
        total_ads = sum((p.ads_spend_ars for p in platforms), Decimal("0")).quantize(_cents)

        # ── Ads vs ventas ──────────────────────────────────────────────────────
        revenue = Decimal(
            str(
                await SaleRepository(self._session).total_revenue(
                    tenant_id, from_date=from_date, to_date=to_date
                )
            )
        ).quantize(_cents)
        ratio: float | None = None
        if revenue > 0:
            ratio = float(total_ads / revenue)

        return MarketingDashboardResponse(
            days=days,
            from_date=from_date,
            to_date=to_date,
            has_data=bool(metrics),
            platforms=platforms,
            total_followers=total_followers,
            total_reach=total_reach,
            total_engagement=total_engagement,
            total_ads_spend_ars=total_ads,
            ads_vs_sales=AdsVsSales(
                ads_spend_ars=total_ads,
                revenue_ars=revenue,
                ratio=ratio,
            ),
        )
