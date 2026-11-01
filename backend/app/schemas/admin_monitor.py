"""Schemas de `/admin/monitor` (panel SUPERADMIN, monitor de plataforma)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel

#: Por qué un negocio aparece en "atención requerida". Cerrado: el frontend
#: mapea cada código a un rótulo y no interpreta texto libre.
MotivoDeAtencion = Literal["GRACE", "READ_ONLY", "TRIAL_ENDING", "INACTIVE"]


class MonitorNegocios(BaseModel):
    """Negocios REALES (sin demos) por estado de cuenta."""

    total: int
    active: int
    trial: int
    read_only: int
    suspended: int


class MonitorSuscripciones(BaseModel):
    """Suscripciones VIVAS (sin canceladas) de negocios reales, por estado."""

    trial: int
    active: int
    grace: int
    read_only: int
    #: Negocios reales sin ninguna suscripción viva. Es un dato roto (sin
    #: `Subscription` el tenant recibe 503): se muestra, no se esconde.
    sin_suscripcion: int
    #: Períodos pagos (`ACTIVE`) que vencen en los próximos 7 días.
    vencen_en_7_dias: int


class MonitorActividad(BaseModel):
    """Negocios reales con al menos un usuario activo (sin contar al soporte)."""

    hoy: int
    ultimos_7_dias: int
    ultimos_30_dias: int


class MonitorIA(BaseModel):
    """Consumo de IA de la plataforma (global, demos incluidos: el costo es real)."""

    dias: int
    tokens_total: int
    cost_usd: float
    #: Tokens sin precio mapeado: si es > 0, `cost_usd` subestima el real.
    unpriced_tokens: int
    #: Fecha del último turno con tokens, en cualquier momento. `None` = nunca.
    ultimo_consumo: date | None


class MonitorAtencion(BaseModel):
    tenant_id: uuid.UUID
    display_name: str
    motivo: MotivoDeAtencion
    #: Fecha que explica el motivo (fin de gracia, fin de prueba, último uso…).
    #: `None` cuando no hay fecha que mostrar (p. ej. nunca tuvo uso).
    fecha: datetime | date | None


class MonitorResponse(BaseModel):
    generado_en: datetime
    negocios: MonitorNegocios
    suscripciones: MonitorSuscripciones
    actividad: MonitorActividad
    ia: MonitorIA
    atencion: list[MonitorAtencion]
    #: Cuántos ítems de atención había en total antes de recortar la lista.
    atencion_total: int
