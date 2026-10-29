"""Schemas de `/admin/tenants` (panel SUPERADMIN, F2)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

#: Los cuatro estados que el panel puede asignar (`domain/tenant_status.py`).
EstadoDeCuenta = Literal["ACTIVE", "TRIAL", "READ_ONLY", "SUSPENDED"]


class AdminTenantListItem(BaseModel):
    tenant_id: uuid.UUID
    display_name: str
    legal_name: str
    vertical_code: str | None
    #: Estado de CUENTA (`tenants.status`), decidido por el soporte.
    account_status: str
    status_changed_at: datetime | None
    is_demo: bool
    created_at: datetime
    #: Estado de SUSCRIPCIÓN, independiente del anterior. `None` = el tenant no
    #: tiene ninguna suscripción viva (nunca tuvo, o está cancelada).
    subscription_plan: str | None
    subscription_status: str | None
    current_period_end: datetime | None
    users_count: int
    #: Último inicio de sesión conocido de cualquiera de sus usuarios. NO es
    #: actividad real: ésa es `last_activity_date` (telemetría diaria, F5).
    last_login_at: datetime | None
    tokens_30d: int
    #: Último día (hora Argentina) con al menos una petición de un usuario del
    #: negocio, sin contar al soporte. `None` = sin uso registrado todavía.
    last_activity_date: date | None
    #: Días distintos con uso en los últimos 30 (de 0 a 30).
    active_days_30d: int


class AdminTenantListResponse(BaseModel):
    items: list[AdminTenantListItem]
    total: int
    page: int
    page_size: int


class AdminTenantSubscription(BaseModel):
    plan_code: str
    status: str
    current_period_start: datetime | None
    current_period_end: datetime | None
    trial_ends_at: datetime | None
    grace_ends_at: datetime | None
    cancel_at_period_end: bool
    seats_included: int
    granted_ia_queries_per_month: int | None
    granted_imports_per_month: int | None
    granted_photo_pdf_reads_per_month: int | None


class AdminTenantUser(BaseModel):
    user_id: uuid.UUID
    email: str
    full_name: str
    role_code: str
    is_active: bool
    last_login_at: datetime | None


class AdminTenantAuditEvent(BaseModel):
    decision_type: str
    created_at: datetime
    actor_user_id: uuid.UUID | None
    decision_data: dict[str, Any]


class AdminTenantDetail(BaseModel):
    tenant_id: uuid.UUID
    display_name: str
    legal_name: str
    vertical_code: str | None
    account_status: str
    status_changed_at: datetime | None
    is_demo: bool
    created_at: datetime
    last_activity_date: date | None
    active_days_30d: int
    subscription: AdminTenantSubscription | None
    users: list[AdminTenantUser]
    recent_events: list[AdminTenantAuditEvent]


class TenantStatusChangeRequest(BaseModel):
    status: EstadoDeCuenta
    #: Por qué. Queda en la auditoría junto al estado anterior y al nuevo.
    reason: str = Field(min_length=3, max_length=300)


class TenantStatusChangeResponse(BaseModel):
    tenant_id: uuid.UUID
    previous_status: str
    status: str
    #: `False` = ya estaba en ese estado: no se tocó nada ni se auditó otra vez.
    changed: bool
    status_changed_at: datetime | None
