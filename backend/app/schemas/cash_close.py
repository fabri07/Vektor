"""Schemas para el cierre de caja diario (arqueo). Sprint 20."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field


class CashMethodBreakdown(BaseModel):
    payment_method: str
    expected_ars: float


class CashClosePreviewResponse(BaseModel):
    close_date: date
    expected_total_ars: float
    breakdown: list[CashMethodBreakdown]
    already_closed: bool
    # True si la hora actual (ART) ya superó el cierre del día laboral y no hay
    # cierre registrado hoy → el frontend destaca el botón.
    is_past_close_now: bool


class CreateCashCloseRequest(BaseModel):
    close_date: date
    counted_total_ars: Decimal = Field(ge=0)
    # Opcional: contado desglosado por método de pago.
    counted_by_method: dict[str, Decimal] | None = None
    notes: str | None = Field(default=None, max_length=500)


class CashCloseResponse(BaseModel):
    id: UUID
    close_date: date
    expected_total_ars: float
    counted_total_ars: float
    difference_ars: float
    breakdown_by_method: dict[str, dict[str, float | None]]
    notes: str | None
    closed_by_user_id: UUID | None
    created_at: datetime
