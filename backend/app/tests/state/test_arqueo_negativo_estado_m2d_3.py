"""M·2d-3: un arqueo negativo es un cierre válido, pero no un saldo de caja.

El contado del arqueo es el neto del día SIN el fondo. Con fondo 100, venta 20 y
retiro 50, el arqueo cuadrado da −30, y en el cajón hay 70. Tomarlo como saldo
dejaba la caja en −30, el score de caja en 0 y CASH_LOW con «$-30 disponible».
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.business_time import today_ar
from app.persistence.models.business import BusinessProfile
from app.persistence.models.cash_close import CashClose
from app.state.business_state_service import compute_business_state
from app.tests.state.test_business_state_service import FakeRedis


async def _perfil_viejo(db: AsyncSession, bp: BusinessProfile) -> None:
    # Sin estimado de caja del alta: el tier «onboarding» no tapa el resultado.
    bp.cash_on_hand_estimate_ars = None
    bp.updated_at = datetime.now(UTC) - timedelta(days=30)
    await db.commit()


async def _arqueo(db: AsyncSession, tenant_id, contado: str) -> None:
    db.add(
        CashClose(
            tenant_id=tenant_id,
            close_date=today_ar(),
            expected_total_ars=Decimal(contado),
            counted_total_ars=Decimal(contado),
            difference_ars=Decimal("0"),
            breakdown_by_method={},
            result_code="BALANCED",
        )
    )
    await db.commit()


async def _estado(db: AsyncSession, tenant_id):
    return await compute_business_state(
        tenant_id=tenant_id, session=db, redis=FakeRedis()  # type: ignore[arg-type]
    )


async def test_un_arqueo_negativo_no_es_saldo_ni_caja_observada(
    db_session: AsyncSession, sample_tenant, sample_business_profile: BusinessProfile
) -> None:
    await _perfil_viejo(db_session, sample_business_profile)
    sin_arqueo = await _estado(db_session, sample_tenant.tenant_id)
    await _arqueo(db_session, sample_tenant.tenant_id, "-30.00")

    state = await _estado(db_session, sample_tenant.tenant_id)

    assert state.cash_source != "arqueo"
    assert state.cash_on_hand_est >= 0
    # V-26 (revisión #227): un arqueo que no define el saldo no lo usa el
    # motor, así que tampoco suma como caja observada. Antes sí sumaba.
    assert state.data_completeness_score == sin_arqueo.data_completeness_score


async def test_un_arqueo_positivo_sigue_siendo_el_saldo(
    db_session: AsyncSession, sample_tenant, sample_business_profile: BusinessProfile
) -> None:
    await _perfil_viejo(db_session, sample_business_profile)
    await _arqueo(db_session, sample_tenant.tenant_id, "100.00")

    state = await _estado(db_session, sample_tenant.tenant_id)

    assert state.cash_source == "arqueo"
    assert state.cash_on_hand_est == Decimal("100.00")


async def test_solo_el_arqueo_positivo_suma_a_la_completitud(
    db_session: AsyncSession, sample_tenant, sample_business_profile: BusinessProfile
) -> None:
    await _perfil_viejo(db_session, sample_business_profile)
    await _arqueo(db_session, sample_tenant.tenant_id, "-30.00")
    negativo = await _estado(db_session, sample_tenant.tenant_id)
    # Mismo día, mismo tenant: se reemplaza el contado.
    from sqlalchemy import update  # noqa: PLC0415

    await db_session.execute(
        update(CashClose)
        .where(CashClose.tenant_id == sample_tenant.tenant_id)
        .values(counted_total_ars=Decimal("100.00"))
    )
    await db_session.commit()
    positivo = await _estado(db_session, sample_tenant.tenant_id)

    # V-26 (revisión #227): el positivo define el saldo y suma la caja; el
    # negativo no (antes valían lo mismo).
    assert positivo.data_completeness_score > negativo.data_completeness_score


async def test_un_arqueo_nuevo_invalida_el_cache_del_estado(
    db_session: AsyncSession, sample_tenant, sample_business_profile: BusinessProfile
) -> None:
    """El último arqueo define la caja: con el MISMO Redis, uno nuevo no se sirve viejo."""
    from sqlalchemy import update  # noqa: PLC0415

    await _perfil_viejo(db_session, sample_business_profile)
    redis = FakeRedis()
    await _arqueo(db_session, sample_tenant.tenant_id, "100.00")
    antes = await compute_business_state(
        tenant_id=sample_tenant.tenant_id, session=db_session, redis=redis  # type: ignore[arg-type]
    )
    assert antes.cash_source == "arqueo"
    await db_session.execute(
        update(CashClose)
        .where(CashClose.tenant_id == sample_tenant.tenant_id)
        .values(counted_total_ars=Decimal("-30.00"))
    )
    await db_session.commit()
    despues = await compute_business_state(
        tenant_id=sample_tenant.tenant_id, session=db_session, redis=redis  # type: ignore[arg-type]
    )
    assert despues.cash_source != "arqueo"
