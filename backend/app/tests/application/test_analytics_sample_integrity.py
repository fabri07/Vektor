"""La muestra de `analytics_events` no se auto-envenena ni reemplaza al benchmark.

Dos garantías, las dos con historia:

1. Un negocio sin ventas escribía `margin_ratio = 0.0` en vez de NULL, y el
   filtro de percentiles solo excluía NULL. Cada negocio vacío entraba como una
   observación de "margen 0%" y arrastraba p10/p25/p50/p75 del rubro entero.
2. El benchmark data-driven contaba EVENTOS y no negocios, así que un solo
   negocio recalculado cinco veces desplazaba el benchmark normativo del rubro
   por la mediana de sí mismo. Ya no puntúa: `recalculate_for_tenant` no puede
   terminar usando otro benchmark que el estático o el override del tenant.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.health_score_service import HealthScoreService
from app.persistence.models.analytics_event import AnalyticsEvent
from app.persistence.models.business import BusinessProfile
from app.persistence.models.tenant import Tenant


@pytest_asyncio.fixture
async def perfil_sin_ventas(
    db_session: AsyncSession, sample_business_profile: BusinessProfile
) -> BusinessProfile:
    """Perfil sin ventas ni estimación de ventas: no hay margen calculable."""
    profile = sample_business_profile
    profile.monthly_sales_estimate_ars = Decimal("0.00")
    profile.monthly_inventory_spend_estimate_ars = Decimal("0.00")
    profile.monthly_fixed_expenses_estimate_ars = Decimal("0.00")
    profile.cash_on_hand_estimate_ars = Decimal("0.00")
    profile.onboarding_completed = True
    profile.updated_at = datetime.now(UTC)
    await db_session.commit()
    return profile


@pytest.mark.asyncio
async def test_sin_ventas_no_registra_un_margen_de_cero(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    perfil_sin_ventas: BusinessProfile,
) -> None:
    """Sin ventas, el evento debe guardar NULL — no un 0.0 que parezca margen real."""
    svc = HealthScoreService(db_session)
    await svc.recalculate_for_tenant(tenant_id=sample_tenant.tenant_id, triggered_by="test")
    await db_session.commit()

    eventos = (await db_session.execute(select(AnalyticsEvent))).scalars().all()
    assert len(eventos) == 1
    assert eventos[0].margin_ratio is None, "un negocio sin ventas no tiene margen 0%, no tiene"
    assert eventos[0].cash_ratio is None


@pytest.mark.asyncio
async def test_con_ventas_si_registra_el_margen(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sample_business_profile: BusinessProfile,
) -> None:
    """Contrapeso del test anterior: con ventas reales el ratio SÍ se registra.

    Sin esto, poner `margin_ratio = None` a secas también pasaría el test de
    arriba y el moat se quedaría sin datos para siempre.
    """
    sample_business_profile.monthly_sales_estimate_ars = Decimal("150000.00")
    sample_business_profile.monthly_inventory_spend_estimate_ars = Decimal("90000.00")
    sample_business_profile.monthly_fixed_expenses_estimate_ars = Decimal("20000.00")
    sample_business_profile.cash_on_hand_estimate_ars = Decimal("15000.00")
    sample_business_profile.onboarding_completed = True
    sample_business_profile.updated_at = datetime.now(UTC)
    await db_session.commit()

    svc = HealthScoreService(db_session)
    await svc.recalculate_for_tenant(tenant_id=sample_tenant.tenant_id, triggered_by="test")
    await db_session.commit()

    evento = (await db_session.execute(select(AnalyticsEvent))).scalars().one()
    assert evento.margin_ratio is not None
    assert evento.cash_ratio is not None


@pytest.mark.asyncio
async def test_el_servicio_de_analytics_no_expone_un_benchmark(
    db_session: AsyncSession,
) -> None:
    """El camino data-driven no puede volver al scoring por descuido.

    `get_data_driven_benchmark` se eliminó y la distribución observada es un tipo
    propio (`ObservedMarginDistribution`), no un `MarginBenchmark`: no se puede
    pasar donde se espera un benchmark ni aunque alguien lo intente.
    """
    from app.application.services.analytics_service import AnalyticsService
    from app.heuristics.verticals import MarginBenchmark
    from app.persistence.repositories.analytics_repository import ObservedMarginDistribution

    assert not hasattr(AnalyticsService(db_session), "get_data_driven_benchmark")
    assert not issubclass(ObservedMarginDistribution, MarginBenchmark)
