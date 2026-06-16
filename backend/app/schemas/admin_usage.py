"""
Pydantic schemas para el dashboard de consumo de tokens (SUPERADMIN).

NOTA sobre `cost_usd`: se expone como ``float`` (no Decimal). El cálculo interno
usa Decimal para precisión monetaria, pero Pydantic v2 serializa Decimal como
string en JSON y eso rompe el frontend numérico — por eso convertimos a float al
construir el response.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from pydantic import BaseModel


class UsageTotals(BaseModel):
    tokens_input: int
    tokens_output: int
    tokens_total: int
    cost_usd: float
    decisions: int


class AgentUsage(BaseModel):
    agent: str
    tokens_total: int
    cost_usd: float


class ModelUsage(BaseModel):
    model: str
    tokens_input: int
    tokens_output: int
    cost_usd: float
    priced: bool


class DayUsage(BaseModel):
    date: date
    tokens_total: int
    cost_usd: float


class TenantUsage(BaseModel):
    tenant_id: UUID
    tokens_total: int
    cost_usd: float


class UsageDashboardResponse(BaseModel):
    days: int
    from_date: date
    to_date: date
    totals: UsageTotals
    by_agent: list[AgentUsage]
    by_model: list[ModelUsage]
    by_day: list[DayUsage]
    by_tenant: list[TenantUsage]
