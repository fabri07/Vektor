"""La muestra de `analytics_events` no se auto-envenena ni reemplaza al benchmark.

Tres garantías, las tres con historia:

1. Un negocio sin ventas escribía `margin_ratio = 0.0` en vez de NULL, y el
   filtro de percentiles solo excluía NULL. Cada negocio vacío entraba como una
   observación de "margen 0%" y arrastraba p10/p25/p50/p75 del rubro entero.
2. El benchmark data-driven contaba EVENTOS y no negocios, así que un solo
   negocio recalculado cinco veces desplazaba el benchmark normativo del rubro
   por la mediana de sí mismo. Ya no puntúa: `recalculate_for_tenant` no puede
   terminar usando otro benchmark que el estático o el override del tenant.
3. Las filas escritas ANTES del fix (1) siguen en la tabla y su cero fabricado es
   indistinguible de un cero genuino. Se las descarta por `schema_version`, que
   marca qué contrato escribió cada fila. El primer intento fue un corte por
   FECHA, que hacía a la corrección dependiente de cuándo ocurriera el deploy:
   si el deploy caía después de la fecha elegida, las filas viejas volvían a
   entrar y nada fallaba.

El efecto de (3) sobre los percentiles necesita `percentile_cont`, que SQLite no
tiene: vive en `app/tests/integration/test_analytics_schema_version_pg.py`. Acá
se cubre lo que sí es SQL portable — el marcado y el filtro de la ventana.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.health_score_service import HealthScoreService
from app.persistence.models.analytics_event import EVENT_SCHEMA_VERSION, AnalyticsEvent
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
async def test_el_evento_nace_marcado_con_la_version_vigente(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sample_business_profile: BusinessProfile,
) -> None:
    """Un evento escrito hoy queda marcado como v2 sin que nadie lo pida.

    El marcado sale del default del ORM, no de una asignación en el servicio: si
    alguien agrega otro camino de escritura, hereda la versión correcta en vez de
    nacer sin marcar y quedar descartado para siempre por los lectores.
    """
    svc = HealthScoreService(db_session)
    await svc.recalculate_for_tenant(tenant_id=sample_tenant.tenant_id, triggered_by="test")
    await db_session.commit()

    evento = (await db_session.execute(select(AnalyticsEvent))).scalars().one()
    assert evento.schema_version == EVENT_SCHEMA_VERSION


async def _sembrar_eventos(
    db_session: AsyncSession, *, vertical_code: str, schema_version: int, cantidad: int
) -> None:
    """`cantidad` eventos con margen real, marcados con la versión pedida."""
    for i in range(cantidad):
        db_session.add(
            AnalyticsEvent(
                vertical_code=vertical_code,
                score_total=70,
                score_cash=70,
                score_margin=70,
                score_stock=70,
                score_supplier=70,
                margin_ratio=0.10 + i / 100,
                cash_ratio=1.5,
                supplier_count=3,
                product_count=10,
                low_stock_pct=0.0,
                data_completeness=80.0,
                schema_version=schema_version,
                created_at=datetime.now(UTC),
            )
        )
    await db_session.commit()


@pytest.mark.asyncio
async def test_un_rubro_con_solo_eventos_viejos_no_llega_a_la_vista(
    db_session: AsyncSession,
) -> None:
    """Sobran eventos para el mínimo de muestra, pero son todos v1: no cuentan.

    Sin el filtro por versión, seis eventos alcanzan de sobra el mínimo de cinco
    y el rubro aparecería con una distribución construida sobre ceros fabricados.
    """
    from app.persistence.repositories.analytics_repository import AnalyticsRepository

    await _sembrar_eventos(
        db_session, vertical_code="kiosco_almacen", schema_version=1, cantidad=6
    )
    repo = AnalyticsRepository(db_session)

    assert await repo.observed_margin_distribution("kiosco_almacen") is None
    assert "kiosco_almacen" not in await repo.get_distinct_verticals()


@pytest.mark.asyncio
async def test_un_rubro_con_eventos_nuevos_si_llega_a_la_vista(
    db_session: AsyncSession,
) -> None:
    """Contrapeso del anterior: el filtro descarta por VERSIÓN, no por todo.

    Sin este test, borrar la tabla entera —o filtrar por una versión imposible—
    también dejaría pasar al de arriba.
    """
    from app.persistence.repositories.analytics_repository import AnalyticsRepository

    await _sembrar_eventos(
        db_session, vertical_code="kiosco_almacen", schema_version=EVENT_SCHEMA_VERSION, cantidad=6
    )

    assert "kiosco_almacen" in await AnalyticsRepository(db_session).get_distinct_verticals()


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
