"""Administración de tenants para el panel SUPERADMIN (F2).

Todo acá lee con `tenant_id` EXPLÍCITO (path de la ruta): el SUPERADMIN cruza
tenants a propósito y nunca por el JWT. El cambio de estado y su auditoría van en
la MISMA transacción: la sesión de la petición commitea al final o hace rollback
de las dos cosas juntas.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.activity_tracker import resumen_de_actividad
from app.domain.subscription import TERMINAL_STATUSES
from app.domain.tenant_status import ESTADOS_ASIGNABLES, normalizar
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.business import BusinessProfile
from app.persistence.models.tenant import Subscription, Tenant
from app.persistence.models.user import User
from app.persistence.repositories.tenant_repository import TenantRepository
from app.schemas.admin_tenants import (
    AdminTenantAuditEvent,
    AdminTenantDetail,
    AdminTenantListItem,
    AdminTenantListResponse,
    AdminTenantSubscription,
    AdminTenantUser,
    TenantStatusChangeResponse,
)

TENANT_STATUS_CHANGED = "TENANT_STATUS_CHANGED"
MAX_PAGE_SIZE = 100
_EVENTOS_EN_LA_FICHA = 20


async def list_tenants(
    session: AsyncSession,
    *,
    page: int,
    page_size: int,
    search: str | None,
    account_status: str | None,
) -> AdminTenantListResponse:
    """Una página de tenants con sus agregados, en un número FIJO de consultas.

    Los agregados (usuarios, último login, tokens) son subconsultas escalares
    correlacionadas: el costo no crece con la cantidad de tenants de la página.
    """
    hace_30 = datetime.now(UTC) - timedelta(days=30)
    users_count = (
        select(func.count(User.user_id))
        .where(User.tenant_id == Tenant.tenant_id)
        .correlate(Tenant)
        .scalar_subquery()
    )
    last_login = (
        select(func.max(User.last_login_at))
        .where(User.tenant_id == Tenant.tenant_id)
        .correlate(Tenant)
        .scalar_subquery()
    )
    tokens_30d = (
        select(func.coalesce(func.sum(DecisionAuditLog.tokens_total), 0))
        .where(
            DecisionAuditLog.tenant_id == Tenant.tenant_id,
            DecisionAuditLog.created_at >= hace_30,
        )
        .correlate(Tenant)
        .scalar_subquery()
    )

    filtros = []
    if search:
        patron = f"%{search.strip().lower()}%"
        filtros.append(
            or_(
                func.lower(Tenant.display_name).like(patron),
                func.lower(Tenant.legal_name).like(patron),
            )
        )
    if account_status:
        filtros.append(func.upper(Tenant.status) == account_status.strip().upper())

    total = (
        await session.scalar(select(func.count()).select_from(Tenant).where(*filtros))
    ) or 0

    # Suscripción VIVA: el índice único parcial garantiza a lo sumo una por tenant,
    # así que el LEFT JOIN no duplica filas.
    stmt = (
        select(
            Tenant,
            BusinessProfile.vertical_code,
            Subscription.plan_code,
            Subscription.status,
            Subscription.current_period_end,
            users_count.label("users_count"),
            last_login.label("last_login_at"),
            tokens_30d.label("tokens_30d"),
        )
        .outerjoin(BusinessProfile, BusinessProfile.tenant_id == Tenant.tenant_id)
        .outerjoin(
            Subscription,
            (Subscription.tenant_id == Tenant.tenant_id)
            & (Subscription.status.not_in(TERMINAL_STATUSES)),
        )
        .where(*filtros)
        .order_by(Tenant.created_at.desc(), Tenant.tenant_id)
        .limit(page_size)
        .offset((page - 1) * page_size)
    )
    filas = (await session.execute(stmt)).all()
    actividad = await resumen_de_actividad(session, [fila[0].tenant_id for fila in filas])
    items = [
        AdminTenantListItem(
            tenant_id=t.tenant_id,
            display_name=t.display_name,
            legal_name=t.legal_name,
            vertical_code=vertical,
            account_status=t.status,
            status_changed_at=t.status_changed_at,
            is_demo=t.is_demo,
            created_at=t.created_at,
            subscription_plan=plan,
            subscription_status=sub_status,
            current_period_end=period_end,
            users_count=int(n_users or 0),
            last_login_at=ult_login,
            tokens_30d=int(tokens or 0),
            last_activity_date=actividad[t.tenant_id][0],
            active_days_30d=actividad[t.tenant_id][1],
        )
        for t, vertical, plan, sub_status, period_end, n_users, ult_login, tokens in filas
    ]
    return AdminTenantListResponse(items=items, total=total, page=page, page_size=page_size)


async def get_tenant_detail(session: AsyncSession, tenant_id: uuid.UUID) -> AdminTenantDetail:
    repo = TenantRepository(session)
    tenant = await repo.get_by_id(tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found.")

    vertical = await session.scalar(
        select(BusinessProfile.vertical_code).where(BusinessProfile.tenant_id == tenant_id)
    )
    sub = await repo.get_current_subscription(tenant_id)
    usuarios = (
        await session.scalars(
            select(User).where(User.tenant_id == tenant_id).order_by(User.created_at)
        )
    ).all()
    eventos = (
        await session.scalars(
            select(DecisionAuditLog)
            .where(DecisionAuditLog.tenant_id == tenant_id)
            .order_by(DecisionAuditLog.created_at.desc())
            .limit(_EVENTOS_EN_LA_FICHA)
        )
    ).all()

    ultimo_uso, dias_activos = (await resumen_de_actividad(session, [tenant.tenant_id]))[
        tenant.tenant_id
    ]
    return AdminTenantDetail(
        tenant_id=tenant.tenant_id,
        display_name=tenant.display_name,
        legal_name=tenant.legal_name,
        vertical_code=vertical,
        account_status=tenant.status,
        status_changed_at=tenant.status_changed_at,
        is_demo=tenant.is_demo,
        created_at=tenant.created_at,
        last_activity_date=ultimo_uso,
        active_days_30d=dias_activos,
        subscription=(
            AdminTenantSubscription(
                plan_code=sub.plan_code,
                status=sub.status,
                current_period_start=sub.current_period_start,
                current_period_end=sub.current_period_end,
                trial_ends_at=sub.trial_ends_at,
                grace_ends_at=sub.grace_ends_at,
                cancel_at_period_end=sub.cancel_at_period_end,
                seats_included=sub.seats_included,
                granted_ia_queries_per_month=sub.granted_ia_queries_per_month,
                granted_imports_per_month=sub.granted_imports_per_month,
                granted_photo_pdf_reads_per_month=sub.granted_photo_pdf_reads_per_month,
            )
            if sub is not None
            else None
        ),
        users=[
            AdminTenantUser(
                user_id=u.user_id,
                email=u.email,
                full_name=u.full_name,
                role_code=u.role_code,
                is_active=u.is_active,
                last_login_at=u.last_login_at,
            )
            for u in usuarios
        ],
        recent_events=[
            AdminTenantAuditEvent(
                decision_type=e.decision_type,
                created_at=e.created_at,
                actor_user_id=e.actor_user_id,
                decision_data=e.decision_data,
            )
            for e in eventos
        ],
    )


async def change_tenant_status(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    new_status: str,
    reason: str,
    actor: User,
) -> TenantStatusChangeResponse:
    """Cambia el estado de cuenta y lo audita en la MISMA transacción.

    Idempotente: pedir el estado que ya tiene no toca nada ni vuelve a auditar,
    así que un reintento (doble click, respuesta perdida) no duplica el efecto.
    La fila se bloquea (`FOR UPDATE`) para que dos cambios simultáneos se
    serialicen y cada auditoría registre el estado anterior REAL.
    """
    if new_status not in ESTADOS_ASIGNABLES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Estado inválido."
        )
    tenant = (
        await session.execute(
            select(Tenant).where(Tenant.tenant_id == tenant_id).with_for_update()
        )
    ).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found.")

    anterior = tenant.status
    if normalizar(anterior) == new_status:
        return TenantStatusChangeResponse(
            tenant_id=tenant_id,
            previous_status=anterior,
            status=tenant.status,
            changed=False,
            status_changed_at=tenant.status_changed_at,
        )

    ahora = datetime.now(UTC)
    tenant.status = new_status
    tenant.status_changed_at = ahora
    session.add(
        DecisionAuditLog(
            tenant_id=tenant_id,
            decision_type=TENANT_STATUS_CHANGED,
            decision_data={
                "previous_status": anterior,
                "new_status": new_status,
                "reason": reason.strip(),
            },
            triggered_by="superadmin",
            actor_user_id=actor.user_id,
            created_at=ahora,
        )
    )
    await session.flush()
    return TenantStatusChangeResponse(
        tenant_id=tenant_id,
        previous_status=anterior,
        status=new_status,
        changed=True,
        status_changed_at=ahora,
    )
