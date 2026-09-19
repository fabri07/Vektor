"""Alta de cuenta (acuñado de tenant) — extraído de `AuthService.register`.

`provision_tenant` es el único lugar que crea las 5 filas de una cuenta nueva:
Tenant → User → Subscription → BusinessProfile → MomentumProfile. Lo llaman
`AuthService.register` y `AccessRequestService.approve`, sin duplicar esta
lógica.

El login con Google NO acuña cuentas y por eso no llama acá: un email
desconocido abre una solicitud de acceso
(`google_oauth_service._resolve_identity`, caso 3), y la cuenta la acuña recién
la aprobación manual — que es la única que conoce el `Vertical` asignado.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.subscription import (
    LEGACY_FREE_LIMITS,
    LEGACY_FREE_PLAN_CODE,
    TRIAL_DAYS,
    SubscriptionStatus,
)
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
    plan_code: str | None = None,
) -> tuple[Tenant, User]:
    """Crea Tenant + User + Subscription + BusinessProfile + MomentumProfile.

    `password_hash=None` genera un hash de contraseña aleatoria
    (`hash_password(str(uuid4()))`) para cuentas cuyo usuario todavía no definió
    su contraseña: la define con el link de invitación del mail de aprobación.

    **`plan_code=None` (el default) sigue acuñando `FREE`/`ACTIVE`, sin cambio
    de comportamiento** — es el camino de `AuthService.register` (demo/altas
    fuera del circuito comercial), que nunca pasa por aprobación manual y no
    tiene un plan asignado que copiar.

    **Con `plan_code` explícito** (el camino de `AccessRequestService.approve`,
    que confirma un `assigned_plan_code` — nunca infiere de `requested_plan`),
    la suscripción nace en `TRIAL`, no en el plan asignado: la política de
    planes exige cupos de prueba fijos e iguales para todos
    (`app.domain.subscription.TRIAL_LIMITS`), nunca los del plan que la
    prueba apunta a convertirse — regalar el plan pago antes de cobrar sería
    justo lo que la versión anterior de este docstring advertía evitar. El
    dueño la activa a mano con `scripts/subscriptions.py activate` cuando
    corresponde cobrar — ver la política de planes en `docs/plans/`.
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

    # 3. Crear Subscription — FREE/ACTIVE (legado) o TRIAL con el plan
    # asignado, según haya o no `plan_code`.
    if plan_code is None:
        subscription = Subscription(
            tenant_id=tenant.tenant_id,
            plan_code=LEGACY_FREE_PLAN_CODE,
            billing_index_reference="MEP",
            seats_included=LEGACY_FREE_LIMITS.seats_included,
            status=SubscriptionStatus.ACTIVE.value,
            granted_ia_queries_per_month=LEGACY_FREE_LIMITS.ia_queries_per_month,
            granted_imports_per_month=LEGACY_FREE_LIMITS.imports_per_month,
            granted_photo_pdf_reads_per_month=LEGACY_FREE_LIMITS.photo_pdf_reads_per_month,
        )
    else:
        ahora = datetime.now(UTC)
        subscription = Subscription(
            tenant_id=tenant.tenant_id,
            plan_code=plan_code,
            billing_index_reference="MEP",
            seats_included=1,
            status=SubscriptionStatus.TRIAL.value,
            trial_ends_at=ahora + timedelta(days=TRIAL_DAYS),
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
