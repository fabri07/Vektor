"""Pydantic schemas para los endpoints de marketing (métricas de redes)."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

Platform = Literal["instagram", "facebook", "tiktok", "other"]


class CreateSocialMetricRequest(BaseModel):
    platform: Platform
    metric_date: date
    followers: int = Field(default=0, ge=0)
    reach: int = Field(default=0, ge=0)
    engagement: int = Field(default=0, ge=0)
    ads_spend_ars: Decimal = Field(default=Decimal("0"), ge=0)


class UpdateSocialMetricRequest(BaseModel):
    platform: Platform | None = None
    metric_date: date | None = None
    followers: int | None = Field(default=None, ge=0)
    reach: int | None = Field(default=None, ge=0)
    engagement: int | None = Field(default=None, ge=0)
    ads_spend_ars: Decimal | None = Field(default=None, ge=0)


class SocialMetricResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    tenant_id: UUID
    platform: str
    metric_date: date
    followers: int
    reach: int
    engagement: int
    ads_spend_ars: Decimal
    created_at: datetime


class PlatformDashboard(BaseModel):
    platform: str
    followers: int  # último valor conocido del período
    reach: int  # suma del período
    engagement: int  # suma del período
    ads_spend_ars: Decimal  # suma del período


class AdsVsSales(BaseModel):
    ads_spend_ars: Decimal
    revenue_ars: Decimal
    ratio: float | None  # ads / revenue; None si revenue == 0


class MarketingDashboardResponse(BaseModel):
    days: int
    from_date: date
    to_date: date
    has_data: bool
    platforms: list[PlatformDashboard]
    total_followers: int
    total_reach: int
    total_engagement: int
    total_ads_spend_ars: Decimal
    ads_vs_sales: AdsVsSales
