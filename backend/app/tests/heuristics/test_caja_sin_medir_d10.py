"""D-10, E0-B-5, E0-B-6: ningún subscore de caja que nadie midió.

- D-10: con saldo (arqueo u onboarding) pero sin gastos fijos, `_score_cash`
  devolvía un 60 «neutral» que pesaba 30% en el total. Ahora, si hay
  movimientos líquidos, se mide la cobertura del flujo (como sin arqueo); si
  no, la caja queda AUSENTE: fuera del total y del riesgo, y se persiste `None`.
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


def test_un_arqueo_no_esconde_la_quema_de_caja_que_el_flujo_mide() -> None:
    """Con saldo, sin fijos y con movimientos líquidos se mide la cobertura del
    flujo, igual que sin arqueo: el dato más confiable no borra una medición."""
    comun: dict[str, Any] = {
        "monthly_fixed_expenses_est": Decimal("0"),
        "liquid_inflow_est": Decimal("50000"),
        "liquid_outflow_est": Decimal("200000"),
        "supplier_count": 5,
    }
    sin_arqueo = calculate_health_score(
        _make_state(cash_source="flujo", cash_on_hand_est=Decimal("0"), **comun)
    )
    con_arqueo = calculate_health_score(
        _make_state(cash_source="arqueo", cash_on_hand_est=Decimal("1000"), **comun)
    )

    assert sin_arqueo.score_cash is not None and sin_arqueo.score_cash <= 14
    assert sin_arqueo.primary_risk_code == "CASH_LOW"
    assert con_arqueo.score_cash == sin_arqueo.score_cash
    assert con_arqueo.primary_risk_code == "CASH_LOW"
    assert con_arqueo.score_total == sin_arqueo.score_total


def test_con_arqueo_sin_fijos_la_dimension_explica_el_flujo() -> None:
    from app.application.services.health_score_service import (  # noqa: PLC0415
        _build_dimensions,
    )

    state = _make_state(
        cash_source="arqueo",
        cash_on_hand_est=Decimal("1000"),
        monthly_fixed_expenses_est=Decimal("0"),
        liquid_inflow_est=Decimal("50000"),
        liquid_outflow_est=Decimal("200000"),
    )

    caja = next(
        d for d in _build_dimensions(state, calculate_health_score(state))
        if d["dimension"] == "cash"
    )

    assert caja["weight"] == "0.30"
    assert "flujo líquido" in str(caja["explanation"])
    assert "no entra al score" not in str(caja["explanation"])


def test_caja_desconocida_queda_fuera_del_total() -> None:
    state = _make_state(cash_source="desconocido", cash_on_hand_est=Decimal("0"))

    result = calculate_health_score(state)

    assert result.score_cash is None
    assert result.margin_status != "ausente"
    assert result.score_margin is not None
    # Renormalización de los otros cuatro: 0,20 + 0,10 + 0,20 + 0,20 = 0,70.
    sin_caja = round(
        (
            result.score_stock * 0.20
            + result.score_supplier * 0.10
            + result.score_margin * 0.20
            + result.score_growth * 0.20
        )
        / 0.70
    )
    assert state.data_completeness_score >= 60  # sin tope por completitud
    assert result.score_total == sin_caja
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


def test_con_saldo_sin_fijos_el_insight_no_inventa_dias_de_cobertura() -> None:
    """Con arqueo y sin gastos fijos, CASH_LOW dice el flujo medido: el código
    base afirmaba «cobertura para 0 días» o «Situación crítica» sobre una
    división que no se pudo hacer."""
    state = SimpleNamespace(
        cash_source="arqueo",
        cash_on_hand_est=Decimal("1000"),
        monthly_fixed_expenses_est=Decimal("0"),
        liquid_inflow_est=Decimal("50000"),
        liquid_outflow_est=Decimal("200000"),
    )

    titulo, descripcion, _ = render_insight("CASH_LOW", state, SimpleNamespace(score_cash=5))

    assert " 0 días" not in titulo + descripcion
    assert "cobertura para" not in descripcion
    assert "Situación crítica" not in descripcion
    assert "$50.000" in descripcion and "$200.000" in descripcion
    # Hay un saldo medido: no se le pide al dueño un arqueo que ya hizo.
    assert "hacé el arqueo" not in descripcion
    assert "gastos fijos" in descripcion


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


# ── Lectores: chat e informe ──────────────────────────────────────────────────


def test_el_chat_dice_sin_medir_y_no_none() -> None:
    from app.application.agents.chat.agent import AgentChat  # noqa: PLC0415
    from app.application.agents.shared.schemas import (  # noqa: PLC0415
        AgentResponse,
        RiskLevel,
    )

    respuesta = AgentResponse(
        request_id=str(uuid.uuid4()),
        agent_name="agent_health",
        status="success",
        risk_level=RiskLevel.LOW,
        result={"components": {"cash": None, "stock": 70}},
    )

    texto = AgentChat._format_agent_result(respuesta)

    assert "  cash: sin medir" in texto
    assert "None" not in texto
    assert "  stock: 70" in texto


def _snapshot_sin_caja() -> HealthScoreSnapshot:
    ahora = datetime(2026, 10, 28, 10, 0, 0)
    return HealthScoreSnapshot(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        total_score=Decimal("70"),
        level="FAIR",
        dimensions=[],
        triggered_by="test",
        snapshot_date=ahora,
        created_at=ahora,
        score_cash=None,
        score_margin=70,
        score_stock=70,
        score_supplier=70,
        score_growth=70,
        primary_risk_code="SUPPLIER_DEPENDENCY",
        confidence_level="MEDIUM",
        data_completeness_score=Decimal("70"),
    )


def test_el_docx_muestra_la_caja_sin_medir() -> None:
    import io  # noqa: PLC0415

    from docx import Document  # noqa: PLC0415

    from app.application.services.report_export_service import generate_docx  # noqa: PLC0415

    doc = Document(io.BytesIO(generate_docx(_snapshot_sin_caja(), "", "Demo")))

    filas = [[c.text for c in r.cells] for r in doc.tables[0].rows]
    assert ["Caja", "—", "Sin medir"] in filas


def test_el_pdf_muestra_la_caja_sin_medir() -> None:
    import reportlab.platypus as platypus  # noqa: PLC0415

    from app.application.services.report_export_service import generate_pdf  # noqa: PLC0415

    with patch("reportlab.platypus.Table", wraps=platypus.Table) as tabla:
        generate_pdf(_snapshot_sin_caja(), "", "Demo")

    filas = tabla.call_args.args[0]
    assert ["Caja", "—", "Sin medir"] in filas
