"""D-5: el contado de un arqueo viejo no es el saldo de caja de hoy.

El contado es la recaudación neta de UN día. El BSL tomaba el último arqueo, de
cuando fuera, como saldo con el tier más alto: un único arqueo de hace cuatro
meses con $200.000 le ganaba al modo flujo y el score de caja medía una caja
«sana» que no decía nada del presente. Ahora sólo un arqueo de hoy o ayer (AR)
define la caja; uno más viejo cae al tier siguiente, y fuera de la ventana del
estado tampoco cuenta como caja observada para la completitud.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.business_time import today_ar
from app.persistence.models.business import BusinessProfile
from app.persistence.models.cash_close import CashClose
from app.persistence.models.transaction import SaleEntry
from app.state.business_state_service import compute_business_state
from app.tests.state.test_business_state_service import FakeRedis


async def _perfil_viejo(db: AsyncSession, bp: BusinessProfile) -> None:
    # Sin estimado de caja del alta: el tier «onboarding» no tapa el resultado.
    bp.cash_on_hand_estimate_ars = None
    bp.updated_at = datetime.now(UTC) - timedelta(days=30)
    await db.commit()


async def _arqueo(db: AsyncSession, tenant_id, contado: str, fecha: date) -> None:
    db.add(
        CashClose(
            tenant_id=tenant_id,
            close_date=fecha,
            expected_total_ars=Decimal(contado),
            counted_total_ars=Decimal(contado),
            difference_ars=Decimal("0"),
            breakdown_by_method={},
            result_code="BALANCED",
        )
    )
    await db.commit()


async def _venta_en_efectivo(db: AsyncSession, tenant_id) -> None:
    db.add(
        SaleEntry(
            tenant_id=tenant_id,
            amount=Decimal("5000.00"),
            quantity=1,
            transaction_date=today_ar(),
            payment_method="cash",
        )
    )
    await db.commit()


async def _estado(db: AsyncSession, tenant_id, redis: FakeRedis | None = None):
    return await compute_business_state(
        tenant_id=tenant_id,
        session=db,
        redis=redis or FakeRedis(),  # type: ignore[arg-type]
    )


async def test_un_arqueo_de_hace_meses_no_le_gana_al_flujo(
    db_session: AsyncSession, sample_tenant, sample_business_profile: BusinessProfile
) -> None:
    await _perfil_viejo(db_session, sample_business_profile)
    await _venta_en_efectivo(db_session, sample_tenant.tenant_id)
    await _arqueo(
        db_session, sample_tenant.tenant_id, "200000.00", today_ar() - timedelta(days=120)
    )

    state = await _estado(db_session, sample_tenant.tenant_id)

    assert state.cash_source == "flujo"
    assert state.cash_on_hand_est == Decimal("0")


async def test_sin_movimientos_un_arqueo_viejo_deja_la_caja_desconocida(
    db_session: AsyncSession, sample_tenant, sample_business_profile: BusinessProfile
) -> None:
    await _perfil_viejo(db_session, sample_business_profile)
    sin_arqueo = await _estado(db_session, sample_tenant.tenant_id)
    await _arqueo(
        db_session, sample_tenant.tenant_id, "200000.00", today_ar() - timedelta(days=120)
    )

    state = await _estado(db_session, sample_tenant.tenant_id)

    # No se inventa un saldo: la caja queda fuera del score...
    assert state.cash_source == "desconocido"
    assert state.cash_on_hand_est == Decimal("0")
    # ...y un arqueo de fuera de la ventana no es caja observada (invariante 5b).
    assert state.data_completeness_score == sin_arqueo.data_completeness_score


async def test_un_arqueo_de_ayer_sigue_siendo_el_saldo(
    db_session: AsyncSession, sample_tenant, sample_business_profile: BusinessProfile
) -> None:
    await _perfil_viejo(db_session, sample_business_profile)
    await _venta_en_efectivo(db_session, sample_tenant.tenant_id)
    await _arqueo(db_session, sample_tenant.tenant_id, "100.00", today_ar() - timedelta(days=1))

    state = await _estado(db_session, sample_tenant.tenant_id)

    assert state.cash_source == "arqueo"
    assert state.cash_on_hand_est == Decimal("100.00")


async def test_un_arqueo_de_la_ventana_no_es_saldo_pero_es_caja_observada(
    db_session: AsyncSession, sample_tenant, sample_business_profile: BusinessProfile
) -> None:
    await _perfil_viejo(db_session, sample_business_profile)
    sin_arqueo = await _estado(db_session, sample_tenant.tenant_id)
    await _arqueo(db_session, sample_tenant.tenant_id, "100.00", today_ar() - timedelta(days=10))

    state = await _estado(db_session, sample_tenant.tenant_id)

    assert state.cash_source != "arqueo"
    assert state.data_completeness_score > sin_arqueo.data_completeness_score


async def test_el_mismo_contado_en_otra_fecha_invalida_el_cache(
    db_session: AsyncSession, sample_tenant, sample_business_profile: BusinessProfile
) -> None:
    """La fecha del arqueo entra al fingerprint: con el MISMO Redis no se sirve vieja."""
    await _perfil_viejo(db_session, sample_business_profile)
    redis = FakeRedis()
    await _arqueo(db_session, sample_tenant.tenant_id, "100.00", today_ar())
    antes = await _estado(db_session, sample_tenant.tenant_id, redis)
    assert antes.cash_source == "arqueo"

    await db_session.execute(
        update(CashClose)
        .where(CashClose.tenant_id == sample_tenant.tenant_id)
        .values(close_date=today_ar() - timedelta(days=120))
    )
    await db_session.commit()
    despues = await _estado(db_session, sample_tenant.tenant_id, redis)

    assert despues.cash_source != "arqueo"
