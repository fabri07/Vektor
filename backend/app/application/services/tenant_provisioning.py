"""Alta de cuenta (acuñado de tenant) — extraído de `AuthService.register`.

`provision_tenant` es el único lugar que crea las 5 filas de una cuenta nueva:
Tenant → User → Subscription → BusinessProfile → MomentumProfile. Hoy lo llama
`AuthService.register`; la aprobación de una solicitud de acceso (Task 8) lo va
a llamar también, sin duplicar esta lógica.

`google_oauth_service._create_social_user` NO usa esta función: ese camino no
tiene un `Vertical` (no crea `BusinessProfile`) y se elimina por completo
cuando el alta social pase a abrir una solicitud de acceso.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.verticals import Vertical
from app.persistence.models.business import BusinessProfile, MomentumProfile
from app.persistence.models.tenant import Subscription, Tenant
from app.persistence.models.user import User
from app.persistence.repositories.tenant_repository import TenantRepository
from app.persistence.repositories.user_repository import UserRepository
from app.utils.security import hash_password


async def provision_tenant(
    session: AsyncSession,
    *,
    business_name: str,
    email: str,
    full_name: str,
    phone: str | None,
    vertical: Vertical,
    password_hash: str | None,
    is_active: bool = True,
) -> tuple[Tenant, User]:
    """Crea Tenant + User + Subscription + BusinessProfile + MomentumProfile.

    `password_hash=None` genera un hash de contraseña aleatoria
    (`hash_password(str(uuid4()))`) para cuentas cuyo usuario nunca inicia
    sesión con password — mismo patrón que ya usa
    `google_oauth_service._create_social_user`.

    **`Subscription.plan_code` es siempre `"FREE"`, por decisión de producto,
    no por omisión.** La solicitud de acceso le pregunta al visitante si
    quiere una cuenta gratuita o Premium (`requested_plan`), pero hoy no
    existe un plan Premium operativo: no hay cobro, no hay límites
    configurados, no hay features detrás de un flag. La intención del
    visitante se guarda en la solicitud para trazabilidad comercial, pero la
    suscripción que se acuña acá es FREE siempre. No copiar `requested_plan`
    a `plan_code` sin construir antes el circuito comercial completo (cobro +
    límites + features) — eso sería regalar un plan pago sin contrato detrás.
    """
    # 1. Crear Tenant (status ACTIVE)
    tenant = Tenant(
        legal_name=business_name,
        display_name=business_name,
        currency="ARS",
        pricing_reference_mode="MEP",
        status="ACTIVE",
    )
    await TenantRepository(session).save(tenant)

    # 2. Crear User
    final_password_hash = (
        password_hash if password_hash is not None else hash_password(str(uuid4()))
    )
    user = User(
        tenant_id=tenant.tenant_id,
        email=email.lower(),
        full_name=full_name,
        password_hash=final_password_hash,
        role_code="OWNER",
        phone=phone,
        is_active=is_active,
    )
    await UserRepository(session).save(user)

    # 3. Crear Subscription (plan FREE)
    subscription = Subscription(
        tenant_id=tenant.tenant_id,
        plan_code="FREE",
        billing_index_reference="MEP",
        seats_included=1,
        status="ACTIVE",
    )
    session.add(subscription)
    await session.flush()

    # 4. Crear BusinessProfile vacío con vertical_code
    profile = BusinessProfile(
        tenant_id=tenant.tenant_id,
        vertical_code=vertical.value,
        data_mode="M0",
        data_confidence="LOW",
        onboarding_completed=False,
        heuristic_profile_version="v1",
    )
    session.add(profile)
    await session.flush()

    # 5. Crear MomentumProfile vacío
    momentum = MomentumProfile(
        tenant_id=tenant.tenant_id,
        improving_streak_weeks=0,
        milestones_json=[],
        updated_at=datetime.now(UTC),
    )
    session.add(momentum)
    await session.flush()

    return tenant, user
