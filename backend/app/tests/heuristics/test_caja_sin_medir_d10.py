"""D-10, E0-B-5, E0-B-6: ningún subscore de caja que nadie midió.

- D-10: con saldo (arqueo u onboarding) pero sin gastos fijos, `_score_cash`
  devolvía un 60 «neutral» que pesaba 30% en el total. Ahora la caja queda
  AUSENTE: fuera del total y del riesgo, y se persiste `None`.
- E0-B-6: la caja desconocida se persistía como 50 y la narrativa del insight
  lo leía como dato; los demás subscores iban con `or 0`. Ahora la caja sin
  medir se persiste `None` y el prompt dice «sin medir».
- E0-B-5: `or 100` convertía `score_cash=0` (la peor caja) en 100 y el insight
  salía con el texto menos severo.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import sessionmaker

from app.application.services.health_score_service import HealthScoreService
from app.domain.business_time import today_ar
from app.heuristics.health_engine import calculate_health_score
from app.heuristics.insight_templates import render_insight
from app.persistence.models.business import BusinessProfile, Insight
from app.persistence.models.cash_close import CashClose
from app.persistence.models.score import HealthScoreSnapshot
from app.persistence.models.tenant import Tenant
from app.tests.conftest import FakeRedis, add_business_profile
from app.tests.heuristics.test_health_engine import _make_state


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


async def _sin_gastos_fijos(db: AsyncSession, bp: BusinessProfile) -> None:
    bp.cash_on_hand_estimate_ars = None
    bp.monthly_fixed_expenses_estimate_ars = None
    bp.updated_at = datetime.now(UTC) - timedelta(days=30)
    await db.commit()


# ── D-10 ──────────────────────────────────────────────────────────────────────


def test_con_saldo_y_sin_gastos_fijos_la_caja_queda_ausente() -> None:
    state = _make_state(
        cash_source="arqueo",
        cash_on_hand_est=Decimal("100000"),
        monthly_fixed_expenses_est=Decimal("0"),
    )

    result = calculate_health_score(state)

    assert result.score_cash is None
    # El total sale sólo de lo medido, re-normalizado sin el 0,30 de la caja.
    pesos = {"stock": 0.20, "supplier": 0.10, "margin": 0.20, "growth": 0.20}
    valores = {
        "stock": result.score_stock,
        "supplier": result.score_supplier,
        "margin": result.score_margin,
        "growth": result.score_growth,
    }
    presentes = {d: v for d, v in valores.items() if v is not None}
    esperado = round(
        sum(v * pesos[d] for d, v in presentes.items()) / sum(pesos[d] for d in presentes)
    )
    if state.data_completeness_score < 40:
        esperado = min(esperado, 60)
    elif state.data_completeness_score < 60:
        esperado = min(esperado, 75)
    assert result.score_total == esperado
    assert result.primary_risk_code != "CASH_LOW"


async def test_con_arqueo_y_sin_gastos_fijos_se_persiste_caja_ausente_y_la_api_la_da_nula(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sample_business_profile: BusinessProfile,
) -> None:
    await _sin_gastos_fijos(db_session, sample_business_profile)
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
    assert guardado.score_inputs_json["cash_source"] == "arqueo"
    assert guardado.score_cash is None
    caja = next(
        d for d in cast(list[dict[str, Any]], guardado.dimensions) if d["dimension"] == "cash"
    )
    assert caja["value"] is None
    assert caja["weight"] == "0.00"
    assert "neutral" not in caja["explanation"]

    # Un snapshot con la caja sin medir sigue siendo un score, no «CALCULATING».
    resp = await client.get("/api/v1/health-scores/latest", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    cuerpo = resp.json()
    assert cuerpo.get("status") is None
    assert cuerpo["score_cash"] is None
    historial = await client.get("/api/v1/health-scores/history/v2", headers=auth_headers)
    assert historial.status_code == 200, historial.text
    assert [s["score_cash"] for s in historial.json()] == [None]


async def test_caja_desconocida_se_persiste_nula_y_no_50(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sample_business_profile: BusinessProfile,
) -> None:
    await _sin_gastos_fijos(db_session, sample_business_profile)

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
    assert guardado.score_inputs_json["cash_source"] == "desconocido"
    assert guardado.score_cash is None


# ── E0-B-5 ────────────────────────────────────────────────────────────────────


def _estado_con_saldo() -> SimpleNamespace:
    return SimpleNamespace(
        cash_source="arqueo",
        cash_on_hand_est=Decimal("0"),
        monthly_fixed_expenses_est=Decimal("600000"),
    )


def test_score_cash_cero_da_la_variante_critica() -> None:
    _, descripcion, _ = render_insight(
        "CASH_LOW", _estado_con_saldo(), SimpleNamespace(score_cash=0)
    )

    assert descripcion.startswith("Situación crítica")


def test_score_cash_nulo_no_elige_variante_de_severidad() -> None:
    _, descripcion, _ = render_insight(
        "CASH_LOW", _estado_con_saldo(), SimpleNamespace(score_cash=None)
    )

    assert "Situación crítica" not in descripcion
    assert "Margen muy ajustado" not in descripcion


# ── E0-B-6 ────────────────────────────────────────────────────────────────────


class _Mensajes:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    async def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        return SimpleNamespace(content=[SimpleNamespace(text="Narrativa de prueba.")])


async def test_la_narrativa_recibe_caja_sin_medir_y_no_un_numero(
    isolated_db_engine: AsyncEngine,
) -> None:
    factory = sessionmaker(  # type: ignore[call-overload]
        isolated_db_engine, class_=AsyncSession, expire_on_commit=False
    )
    tenant_id = uuid.uuid4()
    async with factory() as s:
        s.add(
            Tenant(
                tenant_id=tenant_id,
                legal_name="Kiosco Sin Caja",
                display_name="Kiosco Sin Caja",
                currency="ARS",
                pricing_reference_mode="MEP",
                status="ACTIVE",
            )
        )
        await s.flush()
        await add_business_profile(s, tenant_id)
        ahora = datetime.now(UTC)
        s.add(
            HealthScoreSnapshot(
                tenant_id=tenant_id,
                total_score=Decimal("62"),
                level="FAIR",
                dimensions=[],
                triggered_by="test",
                snapshot_date=ahora,
                created_at=ahora,
                score_cash=None,
                score_margin=None,
                score_stock=70,
                score_supplier=40,
                score_growth=None,
                primary_risk_code="SUPPLIER_DEPENDENCY",
                confidence_level="MEDIUM",
                data_completeness_score=Decimal("70"),
            )
        )
        await s.commit()

    mensajes = _Mensajes()

    async def _dispose_noop(self: AsyncEngine) -> None:
        return None

    from app.jobs import generate_insight as job  # noqa: PLC0415

    with (
        patch("app.persistence.db.engine_factory.crear_engine", return_value=isolated_db_engine),
        # El job cierra su engine al terminar; acá es el de la prueba.
        patch.object(AsyncEngine, "dispose", _dispose_noop),
        patch("redis.asyncio.Redis.from_url", return_value=FakeRedis()),
        patch(
            "app.integrations.anthropic_client.get_anthropic_async_client",
            return_value=SimpleNamespace(messages=mensajes),
        ),
    ):
        await job._run(str(tenant_id))

    prompt = mensajes.kwargs["messages"][0]["content"]
    assert "Liquidez/Caja: sin medir" in prompt
    assert "Crecimiento de ventas: sin medir" in prompt
    assert "Caja: 50/100" not in prompt
    assert re.search(r"(?<!\d)0/100", prompt) is None
    async with factory() as s:
        insight = (
            await s.execute(select(Insight).where(Insight.tenant_id == tenant_id))
        ).scalar_one()
        assert insight.description == "Narrativa de prueba."
