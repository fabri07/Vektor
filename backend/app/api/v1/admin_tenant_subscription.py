"""
Acciones comerciales sobre la suscripción de un tenant — SUPERADMIN (F3).

POST /api/v1/admin/tenants/{tenant_id}/subscription/payments     — activar / renovar (pago)
POST /api/v1/admin/tenants/{tenant_id}/subscription/change-plan  — plan de la próxima renovación
POST /api/v1/admin/tenants/{tenant_id}/subscription/cancel       — cancelar la renovación
GET  /api/v1/admin/tenants/{tenant_id}/subscription/payments     — pagos acreditados

La regla vive en `subscription_billing_service` (la misma que usa el CLI
`scripts/subscriptions.py`): estas rutas sólo traducen HTTP. No hay `set-status`
de la suscripción acá: es una reparación operativa y sigue siendo del CLI.
`tenant_id` va en el path. Sin gate de suscripción del tenant propio.
"""

from __future__ import annotations

import uuid
from typing import NoReturn

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import require_role
from app.application.services import subscription_billing_service as billing
from app.persistence.db.session import get_db_session
from app.persistence.models.tenant import Subscription, SubscriptionPayment
from app.persistence.models.user import User
from app.persistence.repositories.tenant_repository import TenantRepository
from app.schemas.admin_tenant_subscription import (
    CancelRequest,
    CancelResponse,
    ChangePlanRequest,
    ChangePlanResponse,
    SubscriptionPaymentItem,
    SubscriptionPaymentRequest,
    SubscriptionPaymentResponse,
    SubscriptionPaymentsResponse,
)

router = APIRouter(dependencies=[Depends(require_role("SUPERADMIN"))])

#: Datos que faltan o no existen: se corrigen y se reintenta.
_422 = {"PLAN_REQUIRED", "PLAN_UNKNOWN"}
_PAGOS_EN_LA_FICHA = 50


def _operador(actor: User) -> str:
    """Quién lo hizo, para `triggered_by` / `operator` (límite 100 caracteres)."""
    return f"panel:{actor.user_id}"


def _rechazo(exc: billing.BillingError) -> NoReturn:
    """Una operación comercial rechazada: 422 si falta un dato, 409 si el estado no la admite."""
    codigo = (
        status.HTTP_422_UNPROCESSABLE_ENTITY
        if exc.codigo in _422
        else status.HTTP_409_CONFLICT
    )
    raise HTTPException(status_code=codigo, detail={"code": exc.codigo, "message": exc.mensaje})


async def _suscripcion(session: AsyncSession, tenant_id: uuid.UUID) -> Subscription:
    repo = TenantRepository(session)
    if await repo.get_by_id(tenant_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found.")
    sub = await repo.get_current_subscription(tenant_id)
    if sub is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "NO_SUBSCRIPTION",
                "message": "El tenant no tiene ninguna suscripción.",
            },
        )
    return sub


@router.post(
    "/payments",
    response_model=SubscriptionPaymentResponse,
    summary="Acreditar un pago (activar o renovar), idempotente por referencia",
)
async def apply_payment(
    tenant_id: uuid.UUID,
    body: SubscriptionPaymentRequest,
    actor: User = Depends(require_role("SUPERADMIN")),
    session: AsyncSession = Depends(get_db_session),
) -> SubscriptionPaymentResponse:
    sub = await _suscripcion(session, tenant_id)
    try:
        resultado = await billing.apply_payment(
            session,
            subscription_id=sub.subscription_id,
            expected=body.expected,
            plan_code=body.plan_code,
            reference=body.reference,
            amount_usd=body.amount_usd,
            amount_ars=body.amount_ars,
            operator=_operador(actor),
            notes=body.notes,
        )
    except billing.BillingError as exc:
        _rechazo(exc)
    return SubscriptionPaymentResponse(
        kind=resultado.kind,
        plan_code=resultado.plan_code,
        period_start=resultado.period_start,
        period_end=resultado.period_end,
        already_applied=resultado.ya_aplicado,
    )


@router.post(
    "/change-plan",
    response_model=ChangePlanResponse,
    summary="Programar el plan de la próxima renovación",
)
async def change_plan(
    tenant_id: uuid.UUID,
    body: ChangePlanRequest,
    actor: User = Depends(require_role("SUPERADMIN")),
    session: AsyncSession = Depends(get_db_session),
) -> ChangePlanResponse:
    sub = await _suscripcion(session, tenant_id)
    try:
        plan = await billing.schedule_plan_change(
            session,
            subscription_id=sub.subscription_id,
            plan_code=body.plan_code,
            operator=_operador(actor),
        )
    except billing.BillingError as exc:
        _rechazo(exc)
    return ChangePlanResponse(scheduled_plan_code=plan)


@router.post(
    "/cancel",
    response_model=CancelResponse,
    summary="Cancelar la renovación (conserva lo ya pago)",
)
async def cancel(
    tenant_id: uuid.UUID,
    body: CancelRequest,
    actor: User = Depends(require_role("SUPERADMIN")),
    session: AsyncSession = Depends(get_db_session),
) -> CancelResponse:
    sub = await _suscripcion(session, tenant_id)
    try:
        hasta = await billing.schedule_cancellation(
            session,
            subscription_id=sub.subscription_id,
            operator=_operador(actor),
            reason=body.reason,
        )
    except billing.BillingError as exc:
        _rechazo(exc)
    return CancelResponse(access_until=hasta)


@router.get(
    "/payments",
    response_model=SubscriptionPaymentsResponse,
    summary="Pagos acreditados del tenant (los últimos 50)",
)
async def list_payments(
    tenant_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
) -> SubscriptionPaymentsResponse:
    if await TenantRepository(session).get_by_id(tenant_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found.")
    filas = (
        await session.scalars(
            select(SubscriptionPayment)
            .where(SubscriptionPayment.tenant_id == tenant_id)
            .order_by(SubscriptionPayment.created_at.desc())
            .limit(_PAGOS_EN_LA_FICHA)
        )
    ).all()
    return SubscriptionPaymentsResponse(
        items=[SubscriptionPaymentItem.model_validate(f, from_attributes=True) for f in filas]
    )
