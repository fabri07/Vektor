"""V-23: sin salidas líquidas la caja queda sin medir, no 75.

`_score_cash_coverage` devolvía 75 cuando había entradas y ninguna salida
líquida, y 50 cuando no había nada: sin egresos no hay ratio de cobertura, así
que eran valores inventados que pesaban 30% en el total (no-invention). Desde
D-10 (#224) el mismo camino alcanzaba también al saldo sin gastos fijos. Ahora,
en flujo y con saldo sin fijos, sin salidas líquidas la caja es `None`: fuera
del total (pesos re-normalizados) y del riesgo, se persiste nula, y ni el
insight ni la dimensión dicen que la caja «cubre» algo: piden lo que falta.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.health_score_service import (
    HealthScoreService,
    _build_dimensions,
)
from app.domain.business_time import today_ar
from app.heuristics.health_engine import (
    HealthScoreResult,
    _score_cash_coverage,
    caja_sin_medir,
    calculate_health_score,
    mide_caja_por_flujo,
)
from app.heuristics.insight_templates import render_insight
from app.persistence.models.business import BusinessProfile
from app.persistence.models.cash_close import CashClose
from app.persistence.models.score import HealthScoreSnapshot
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.tests.heuristics.test_health_engine import _make_state

_PESOS_SIN_CAJA = {"stock": 0.20, "supplier": 0.10, "margin": 0.20, "growth": 0.20}


def _total_sin_caja(result: HealthScoreResult) -> int:
    valores = {
        "stock": result.score_stock,
        "supplier": result.score_supplier,
        "margin": result.score_margin,
        "growth": result.score_growth,
    }
    presentes = {d: v for d, v in valores.items() if v is not None}
    return round(
        sum(v * _PESOS_SIN_CAJA[d] for d, v in presentes.items())
        / sum(_PESOS_SIN_CAJA[d] for d in presentes)
    )


# Los dos modos que miden la caja por el flujo: sin saldo, y con saldo sin fijos.
_SOLO_ENTRADAS: dict[str, dict[str, Any]] = {
    "flujo": {
        "cash_source": "flujo",
        "cash_on_hand_est": Decimal("0"),
        "monthly_fixed_expenses_est": Decimal("17000"),
    },
    "arqueo_sin_fijos": {
        "cash_source": "arqueo",
        "cash_on_hand_est": Decimal("100000"),
        "monthly_fixed_expenses_est": Decimal("0"),
    },
}


# ── Motor ─────────────────────────────────────────────────────────────────────


def test_sin_salidas_la_cobertura_no_tiene_valor() -> None:
    assert _score_cash_coverage(Decimal("50000"), Decimal("0")) is None
    assert _score_cash_coverage(Decimal("0"), Decimal("0")) is None
    # Con salidas sigue midiendo: el arreglo no apaga la cobertura.
    assert _score_cash_coverage(Decimal("2000"), Decimal("1000")) is not None


@pytest.mark.parametrize("modo", list(_SOLO_ENTRADAS))
def test_con_entradas_y_sin_salidas_la_caja_queda_fuera_del_total(modo: str) -> None:
    state = _make_state(
        liquid_inflow_est=Decimal("50000"),
        liquid_outflow_est=Decimal("0"),
        **_SOLO_ENTRADAS[modo],
    )

    result = calculate_health_score(state)

    assert result.score_cash is None
    assert state.data_completeness_score >= 60  # sin tope por completitud
    assert result.score_total == _total_sin_caja(result)
    assert result.primary_risk_code != "CASH_LOW"
    assert caja_sin_medir(state)
    assert not mide_caja_por_flujo(state)


@pytest.mark.parametrize("modo", list(_SOLO_ENTRADAS))
def test_con_salidas_la_caja_se_sigue_midiendo_por_el_flujo(modo: str) -> None:
    state = _make_state(
        liquid_inflow_est=Decimal("50000"),
        liquid_outflow_est=Decimal("200000"),
        supplier_count=5,
        **_SOLO_ENTRADAS[modo],
    )

    result = calculate_health_score(state)

    assert mide_caja_por_flujo(state)
    assert not caja_sin_medir(state)
    assert result.score_cash is not None and result.score_cash <= 14
    assert result.primary_risk_code == "CASH_LOW"


def test_con_saldo_y_fijos_las_salidas_no_cambian_nada() -> None:
    """Los días de runway no dependen de las salidas líquidas: con saldo y
    fijos, una caja sin egresos se sigue midiendo como antes."""
    state = _make_state(
        cash_source="arqueo",
        cash_on_hand_est=Decimal("40000"),
        monthly_fixed_expenses_est=Decimal("17000"),
        liquid_inflow_est=Decimal("50000"),
        liquid_outflow_est=Decimal("0"),
    )

    assert not caja_sin_medir(state)
    assert calculate_health_score(state).score_cash is not None


# ── Insight CASH_LOW ──────────────────────────────────────────────────────────


def test_en_flujo_sin_salidas_el_insight_no_dice_que_la_caja_cubre() -> None:
    """Un snapshot con CASH_LOW leído contra un estado de hoy sin salidas: el
    código base decía «cubre poco lo que sale … salieron $0»."""
    state = SimpleNamespace(
        cash_source="flujo",
        cash_on_hand_est=Decimal("0"),
        monthly_fixed_expenses_est=Decimal("17000"),
        liquid_inflow_est=Decimal("50000"),
        liquid_outflow_est=Decimal("0"),
    )

    titulo, descripcion, accion = render_insight(
        "CASH_LOW", state, SimpleNamespace(score_cash=None)
    )

    texto = titulo + descripcion
    assert "cubre" not in texto
    assert "$0" not in texto
    assert "días" not in titulo
    assert "no podemos medir" in descripcion
    assert "$50.000" in descripcion
    assert "pagos y gastos" in accion
    assert "gastos no esenciales" not in accion


def test_con_saldo_sin_fijos_ni_salidas_el_insight_pide_lo_que_falta() -> None:
    state = SimpleNamespace(
        cash_source="arqueo",
        cash_on_hand_est=Decimal("100000"),
        monthly_fixed_expenses_est=Decimal("0"),
        liquid_inflow_est=Decimal("50000"),
        liquid_outflow_est=Decimal("0"),
    )

    titulo, descripcion, accion = render_insight(
        "CASH_LOW", state, SimpleNamespace(score_cash=None)
    )

    texto = titulo + descripcion
    assert "cubre" not in texto
    assert " 0 días" not in texto
    assert "Situación crítica" not in texto
    # Hay saldo medido: no se pide un arqueo que ya está; se piden fijos o salidas.
    assert "arqueo" not in accion
    assert "gastos fijos" in accion


# ── Dimensión ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("modo", list(_SOLO_ENTRADAS))
def test_sin_salidas_la_dimension_no_explica_una_cobertura(modo: str) -> None:
    state = _make_state(
        liquid_inflow_est=Decimal("50000"),
        liquid_outflow_est=Decimal("0"),
        **_SOLO_ENTRADAS[modo],
    )

    caja = next(
        d
        for d in _build_dimensions(state, calculate_health_score(state))
        if d["dimension"] == "cash"
    )

    explicacion = str(caja["explanation"])
    assert caja["value"] is None
    assert caja["weight"] == "0.00"
    assert "se mide la cobertura" not in explicacion
    assert "no entra al score" in explicacion
    assert "salidas" in explicacion


# ── Servicio + API: score persistido ──────────────────────────────────────────


async def _venta_en_efectivo(db: AsyncSession, tenant_id: uuid.UUID) -> None:
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


async def _arqueo_de_hoy(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    db.add(
        CashClose(
            tenant_id=tenant_id,
            close_date=today_ar(),
            expected_total_ars=Decimal("100000.00"),
            counted_total_ars=Decimal("100000.00"),
            difference_ars=Decimal("0"),
            breakdown_by_method={},
            result_code="BALANCED",
        )
    )
    await db.commit()


@pytest.mark.parametrize("con_arqueo", [False, True], ids=["flujo", "arqueo_sin_fijos"])
async def test_solo_entradas_se_persiste_la_caja_nula_y_la_api_la_da_nula(
    con_arqueo: bool,
    client: AsyncClient,
    auth_headers: dict[str, Any],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sample_business_profile: BusinessProfile,
) -> None:
    # Sin estimados del alta: la caja sale del flujo (o del arqueo), no del onboarding.
    sample_business_profile.cash_on_hand_estimate_ars = None
    sample_business_profile.monthly_fixed_expenses_estimate_ars = None
    sample_business_profile.updated_at = datetime.now(UTC) - timedelta(days=30)
    await db_session.commit()
    await _venta_en_efectivo(db_session, sample_tenant.tenant_id)
    if con_arqueo:
        await _arqueo_de_hoy(db_session, sample_tenant.tenant_id)

    snapshot = await HealthScoreService(db_session).recalculate_for_tenant(
        tenant_id=sample_tenant.tenant_id, triggered_by="test"
    )
    await db_session.commit()

    snapshot_id = snapshot.id
    db_session.expire_all()
    guardado = (
        await db_session.execute(
            select(HealthScoreSnapshot).where(HealthScoreSnapshot.id == snapshot_id)
        )
    ).scalar_one()
    assert guardado.score_inputs_json is not None
    assert guardado.score_inputs_json["cash_source"] == ("arqueo" if con_arqueo else "flujo")
    assert Decimal(guardado.score_inputs_json["liquid_inflow_est"]) > 0
    assert Decimal(guardado.score_inputs_json["liquid_outflow_est"]) == 0
    assert guardado.score_cash is None
    assert guardado.primary_risk_code != "CASH_LOW"
    caja = next(
        d for d in cast(list[dict[str, Any]], guardado.dimensions) if d["dimension"] == "cash"
    )
    assert caja["value"] is None
    assert caja["weight"] == "0.00"

    resp = await client.get("/api/v1/health-scores/latest", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    cuerpo = resp.json()
    assert cuerpo.get("status") is None
    assert cuerpo["score_cash"] is None
