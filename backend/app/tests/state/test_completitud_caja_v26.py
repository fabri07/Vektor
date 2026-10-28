"""V-26: la caja sin medir no suma a la completitud.

`_compute_completeness` le daba los 20 puntos de la caja a un negocio con
entradas líquidas solas (`cash_source="flujo"`, `has_liquid_movements`), aunque
el motor deja esa caja sin medir (V-23): sin salidas no hay cobertura. Con ≥10
ventas en efectivo y 5 productos daba 25 + 20 + 10 = 55 → MEDIUM: pasaba la
compuerta de no-invención y mostraba un score calculado sin caja.

Decisión del dueño (2026-10-28): la caja cuenta como OBSERVADA sólo con un
arqueo de la ventana o alguna salida líquida (`hay_salidas_liquidas`, el mismo
predicado que usa el motor). El saldo DECLARADO del alta sigue valiendo
`PESO_DECLARADO` (invariante 5b), pero sólo si el motor lo puede medir (hay
gastos fijos contra los que contar días).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.health_score_service import HealthScoreService
from app.domain.business_time import today_ar
from app.heuristics.health_engine import caja_sin_medir, calculate_health_score
from app.persistence.models.business import BusinessProfile
from app.persistence.models.cash_close import CashClose
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.state.business_state_service import (
    BusinessState,
    compute_business_state,
    hay_salidas_liquidas,
)
from app.tests.heuristics.test_health_engine import _make_state
from app.tests.state.test_business_state_service import FakeRedis


async def _sin_estimados(db: AsyncSession, bp: BusinessProfile) -> None:
    # Sin respuestas del alta: lo que cuente sale de datos cargados.
    bp.monthly_sales_estimate_ars = None
    bp.monthly_inventory_spend_estimate_ars = None
    bp.monthly_fixed_expenses_estimate_ars = None
    bp.cash_on_hand_estimate_ars = None
    bp.supplier_count_estimate = None
    bp.product_count_estimate = None
    bp.updated_at = datetime.now(UTC) - timedelta(days=30)
    await db.commit()


async def _solo_ventas_en_efectivo(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    """El escenario del hallazgo: 12 ventas en efectivo y 5 productos, nada más."""
    hoy = today_ar()
    for i in range(12):
        db.add(
            SaleEntry(
                tenant_id=tenant_id,
                amount=Decimal("5000.00"),
                quantity=1,
                transaction_date=hoy - timedelta(days=(i % 25) + 1),
                payment_method="cash",
            )
        )
    for i in range(5):
        db.add(
            Product(
                tenant_id=tenant_id,
                name=f"Producto {i}",
                sale_price_ars=Decimal("1000.00"),
                stock_units=10,
                is_active=True,
            )
        )
    await db.commit()


async def _gasto_en_efectivo(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    db.add(
        ExpenseEntry(
            tenant_id=tenant_id,
            amount=Decimal("20000.00"),
            description="Alquiler",
            category="RENT",
            expense_type="OPEX",
            is_recurring=True,  # el alquiler es fijo: suma también los 15 de fijos
            transaction_date=today_ar() - timedelta(days=3),
            payment_method="cash",
        )
    )
    await db.commit()


async def _arqueo_de_hoy(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    db.add(
        CashClose(
            tenant_id=tenant_id,
            close_date=today_ar(),
            expected_total_ars=Decimal("60000.00"),
            counted_total_ars=Decimal("60000.00"),
            difference_ars=Decimal("0"),
            breakdown_by_method={},
            result_code="BALANCED",
        )
    )
    await db.commit()


async def _estado(db: AsyncSession, tenant_id: uuid.UUID) -> BusinessState:
    return await compute_business_state(
        tenant_id=tenant_id,
        session=db,
        redis=FakeRedis(),  # type: ignore[arg-type]  # test double / fixture
    )


# ── BSL ───────────────────────────────────────────────────────────────────────


async def test_con_solo_entradas_la_caja_no_suma_y_queda_en_low(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sample_business_profile: BusinessProfile,
) -> None:
    await _sin_estimados(db_session, sample_business_profile)
    await _solo_ventas_en_efectivo(db_session, sample_tenant.tenant_id)

    state = await _estado(db_session, sample_tenant.tenant_id)

    assert state.cash_source == "flujo"
    assert state.liquid_inflow_est > 0
    assert state.liquid_outflow_est == 0
    # Ventas observadas (25) + productos reales (10); la caja, nada. Antes: 55.
    assert state.data_completeness_score == pytest.approx(35.0)
    assert state.confidence_level == "LOW"
    # El motor la deja sin medir: la completitud dice lo mismo.
    assert caja_sin_medir(state)


@pytest.mark.parametrize("carga", ["gasto_en_efectivo", "arqueo"])
async def test_un_gasto_en_efectivo_o_un_arqueo_hacen_observada_la_caja(
    carga: str,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sample_business_profile: BusinessProfile,
) -> None:
    await _sin_estimados(db_session, sample_business_profile)
    await _solo_ventas_en_efectivo(db_session, sample_tenant.tenant_id)
    if carga == "arqueo":
        await _arqueo_de_hoy(db_session, sample_tenant.tenant_id)
        esperado = 35.0 + 20.0
    else:
        await _gasto_en_efectivo(db_session, sample_tenant.tenant_id)
        esperado = 35.0 + 20.0 + 15.0  # caja y gastos fijos, los dos observados

    state = await _estado(db_session, sample_tenant.tenant_id)

    assert state.data_completeness_score == pytest.approx(esperado)
    assert state.confidence_level == "MEDIUM"


async def test_el_saldo_declarado_sin_fijos_ni_salidas_no_suma(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sample_business_profile: BusinessProfile,
) -> None:
    """El saldo del alta vale `PESO_DECLARADO` sólo si el motor lo puede medir:
    sin fijos ni salidas queda sin medir (D-10) y no suma los 8."""
    await _sin_estimados(db_session, sample_business_profile)
    await _solo_ventas_en_efectivo(db_session, sample_tenant.tenant_id)
    sample_business_profile.cash_on_hand_estimate_ars = Decimal("50000.00")
    sample_business_profile.onboarding_completed = True
    sample_business_profile.updated_at = datetime.now(UTC)
    await db_session.commit()

    sin_fijos = await _estado(db_session, sample_tenant.tenant_id)

    assert sin_fijos.cash_source == "onboarding"
    assert caja_sin_medir(sin_fijos)
    assert sin_fijos.data_completeness_score == pytest.approx(35.0)

    # Con fijos declarados el motor mide días sobre el saldo declarado: suma
    # 20 × 0,4 = 8 por la caja y 15 × 0,4 = 6 por los fijos (invariante 5b).
    sample_business_profile.monthly_fixed_expenses_estimate_ars = Decimal("20000.00")
    sample_business_profile.updated_at = datetime.now(UTC)
    await db_session.commit()

    con_fijos = await _estado(db_session, sample_tenant.tenant_id)

    assert not caja_sin_medir(con_fijos)
    assert con_fijos.data_completeness_score == pytest.approx(35.0 + 8.0 + 6.0)


@pytest.mark.parametrize(
    ("entradas", "salidas"),
    [("50000", "0"), ("0", "0"), ("50000", "1000"), ("0", "1000")],
)
def test_el_motor_y_la_completitud_usan_la_misma_regla_de_salidas(
    entradas: str, salidas: str
) -> None:
    state = _make_state(
        cash_source="flujo",
        cash_on_hand_est=Decimal("0"),
        liquid_inflow_est=Decimal(entradas),
        liquid_outflow_est=Decimal(salidas),
    )

    medida = calculate_health_score(state).score_cash is not None
    assert medida == hay_salidas_liquidas(Decimal(salidas))
    assert medida == (not caja_sin_medir(state))


# ── Servicio + API ────────────────────────────────────────────────────────────


async def _latest(client: AsyncClient, headers: dict[str, Any]) -> dict[str, Any]:
    resp = await client.get("/api/v1/health-scores/latest", headers=headers)
    assert resp.status_code == 200, resp.text
    cuerpo: dict[str, Any] = resp.json()
    return cuerpo


async def test_la_api_da_low_con_solo_ventas_y_medium_con_un_gasto_en_efectivo(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sample_business_profile: BusinessProfile,
) -> None:
    await _sin_estimados(db_session, sample_business_profile)
    await _solo_ventas_en_efectivo(db_session, sample_tenant.tenant_id)
    servicio = HealthScoreService(db_session)

    await servicio.recalculate_for_tenant(
        tenant_id=sample_tenant.tenant_id, triggered_by="test"
    )
    await db_session.commit()

    solo_ventas = await _latest(client, auth_headers)
    assert solo_ventas["confidence_level"] == "LOW"
    assert solo_ventas["data_completeness_score"] == pytest.approx(35.0)
    assert solo_ventas["score_cash"] is None

    await _gasto_en_efectivo(db_session, sample_tenant.tenant_id)
    await servicio.recalculate_for_tenant(
        tenant_id=sample_tenant.tenant_id, triggered_by="test"
    )
    await db_session.commit()

    con_gasto = await _latest(client, auth_headers)
    assert con_gasto["confidence_level"] == "MEDIUM"
    assert con_gasto["data_completeness_score"] == pytest.approx(70.0)
    assert con_gasto["score_cash"] is not None
