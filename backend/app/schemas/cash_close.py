"""Schemas para el cierre de caja diario (arqueo). Sprint 20; arqueo completo Sprint 22."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


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
    # Fondo de caja sugerido: el del último cierre con arqueo (None si no hay).
    suggested_opening_float_ars: float | None = None
    # 'registered' | 'informal' | None — solo adapta la guía del modal.
    fiscal_condition: str | None = None


class CreateCashCloseRequest(BaseModel):
    close_date: date
    counted_total_ars: Decimal = Field(ge=0)
    # Opcional: contado desglosado por método de pago.
    counted_by_method: dict[str, Decimal] | None = None
    notes: str | None = Field(default=None, max_length=500)
    # ── Arqueo estructurado (todos opcionales; el flujo viejo sigue andando) ──
    # Fondo con el que abrió el día — fijo, no es ganancia.
    opening_float_ars: Decimal | None = Field(default=None, ge=0)
    # {denominación: cantidad} — ej {"20000": 3}. El efectivo físico se computa
    # server-side desde acá cuando viene.
    cash_denominations: dict[str, int] | None = None
    # Vales: salidas de caja con comprobante casero NO registradas como gasto.
    voucher_expenses_ars: Decimal | None = Field(default=None, ge=0)
    # Si hay sobrante, registrarlo como otros ingresos (inflow).
    register_surplus_as_income: bool = False

    @field_validator("cash_denominations")
    @classmethod
    def _denominations_validas(
        cls, value: dict[str, int] | None
    ) -> dict[str, int] | None:
        if value is None:
            return None
        for denom, qty in value.items():
            try:
                if float(denom) <= 0:
                    raise ValueError
            except ValueError as exc:
                raise ValueError(f"Denominación inválida: {denom!r}") from exc
            if qty < 0:
                raise ValueError(f"Cantidad negativa para la denominación {denom}")
        return value


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
    opening_float_ars: float | None = None
    cash_denominations: dict[str, int] | None = None
    voucher_expenses_ars: float | None = None
    result_code: Literal["BALANCED", "SURPLUS", "SHORTAGE"] | None = None
    surplus_registered: bool = False
