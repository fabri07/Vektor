"""Schemas de las acciones comerciales del panel SUPERADMIN (F3)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field


class SubscriptionPaymentRequest(BaseModel):
    """Acredita UN mes. Activar y renovar son la misma operación con distinta intención."""

    #: Lo que quien llama CREE estar haciendo; el servicio lo verifica contra el
    #: estado real y responde 409 `USE_RENEW`/`USE_ACTIVATE` si no coincide.
    expected: Literal["activate", "renew"]
    #: Clave de idempotencia (junto con la fuente): repetirla con los MISMOS datos
    #: devuelve lo ya acreditado; con otros datos, 409 `REFERENCE_MISMATCH`.
    reference: str = Field(min_length=3, max_length=200)
    #: Lo que el cliente pagó, en pesos.
    amount_ars: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    #: Pesos por dólar con que se convierte el pago. El servidor deriva el
    #: equivalente en USD (`amount_ars / exchange_rate`, 2 decimales): el operador
    #: no tipea un importe en USD que pueda no cerrar con los otros dos.
    exchange_rate: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    #: Obligatorio al activar; al renovar, por defecto el plan programado o el vigente.
    plan_code: str | None = Field(default=None, max_length=50)
    notes: str | None = Field(default=None, max_length=300)


class SubscriptionPaymentResponse(BaseModel):
    kind: str
    plan_code: str
    period_start: datetime
    period_end: datetime
    #: `True` = esa referencia ya estaba acreditada: no se otorgó nada nuevo.
    already_applied: bool


class ChangePlanRequest(BaseModel):
    plan_code: str = Field(min_length=1, max_length=50)


class ChangePlanResponse(BaseModel):
    #: Plan de la PRÓXIMA renovación. No cobra ni toca el período en curso.
    scheduled_plan_code: str


class CancelRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=300)


class CancelResponse(BaseModel):
    #: Hasta cuándo conserva el acceso pago: cancelar la renovación no corta lo ya pago.
    access_until: datetime


class SubscriptionPaymentItem(BaseModel):
    id: uuid.UUID
    source: str
    reference: str
    kind: str
    plan_code: str
    amount_usd: Decimal
    amount_ars: Decimal
    exchange_rate: Decimal | None = None
    operator: str
    period_start: datetime
    period_end: datetime
    notes: str | None
    created_at: datetime


class SubscriptionPaymentsResponse(BaseModel):
    items: list[SubscriptionPaymentItem]
