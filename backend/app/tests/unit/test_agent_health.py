"""Unit tests for AgentHealth — scorer determinístico + narrativa Sonnet.

Stage 5a: AgentHealth refactorizado a thin coordinator.
  - scorer.py (compat shim) → ComponentScores v1 (tests de fórmula legacy)
  - sub_calculator.py → ComponentScoresV2 (tests de fórmula v2 y alertas)
  - sub_narrator.py → genera narrativa
  - agent.py → thin coordinator (tests de process())
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.application.agents.health.scorer import (
    ComponentScores,
    compute_cash_score,
    compute_discipline_score,
    compute_health_score,
    compute_stock_score,
    compute_supplier_score,
)
from app.application.agents.health.sub_calculator import ComponentScoresV2
from app.application.agents.shared.heuristic_engine import (
    CashHealthConfig,
    HeuristicConfig,
    HeuristicEngine,
)
from app.application.agents.shared.schemas import AgentRequest
from app.domain.verticals import Vertical

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_config(healthy_days_min: float = 10.0, warning_days_min: float = 7.0) -> HeuristicConfig:
    # Se parte del perfil real de kiosco (los sub-configs ya no tienen defaults)
    # y solo se sobreescribe la parte de caja que el test necesita variar.
    return HeuristicEngine.get(Vertical.KIOSCO_ALMACEN).model_copy(
        update={
            "cash_health": CashHealthConfig(
                healthy_days_min=healthy_days_min,
                warning_days_min=warning_days_min,
                critical_days_below=5.0,
            )
        }
    )


def _make_request(
    message: str = "generar informe de salud", business_id: str = "tenant-456"
) -> AgentRequest:
    """`business_id` es parametrizable porque el agente lo convierte a UUID.

    El default no lo es, y no se toca: los tests viejos dependen de que ese
    `uuid.UUID(...)` falle y el agente caiga al benchmark del rubro. Los tests que
    ejercitan el override del tenant necesitan que la conversión funcione para
    llegar a `get_margin_benchmark`, así que pasan un UUID de verdad.
    """
    return AgentRequest(
        user_id="user-123",
        business_id=business_id,
        message=message,
    )


def _mock_anthropic_client(narrative_text: str = "Narrativa de prueba.") -> MagicMock:
    content_block = MagicMock()
    content_block.text = narrative_text
    usage = MagicMock()
    usage.input_tokens = 100
    usage.output_tokens = 50
    response = MagicMock()
    response.content = [content_block]
    response.usage = usage
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=response)
    return mock_client


def _make_scores_v2(
    cash: int = 70,
    stock: int = 70,
    supplier: int = 70,
    margin: int = 70,
    growth: int = 70,
    total: int = 70,
    confidence_level: str = "HIGH",
    completeness: float = 90.0,
) -> ComponentScoresV2:
    return ComponentScoresV2(
        cash_score=cash,
        stock_score=stock,
        supplier_score=supplier,
        margin_score=margin,
        growth_score=growth,
        total_score=total,
        primary_risk_code="CASH_LOW",
        confidence_level=confidence_level,
        data_completeness_score=completeness,
    )


# ── Tests de fórmula v1 (compat shim scorer.py) ───────────────────────────────


def test_score_formula_correct():
    """components={100,100,100,100} → score=100.0"""
    components = ComponentScores(
        cash_score=100.0,
        stock_score=100.0,
        supplier_score=100.0,
        discipline_score=100.0,
    )
    assert compute_health_score(components) == 100.0


@pytest.mark.parametrize(
    ("cash", "stock", "supplier", "discipline", "esperado"),
    [
        # Un componente en 100 y el resto en 0 → el score ES el peso del componente.
        pytest.param(100.0, 0.0, 0.0, 0.0, 35.0, id="test_score_weights"),
        pytest.param(0.0, 100.0, 0.0, 0.0, 30.0, id="test_score_weight_stock"),
        pytest.param(0.0, 0.0, 100.0, 0.0, 15.0, id="test_score_weight_supplier"),
        pytest.param(0.0, 0.0, 0.0, 100.0, 20.0, id="test_score_weight_discipline"),
    ],
)
def test_score_weights_v1(cash, stock, supplier, discipline, esperado):
    """Fórmula v1: cash×0.35 + stock×0.30 + supplier×0.15 + discipline×0.20."""
    components = ComponentScores(
        cash_score=cash,
        stock_score=stock,
        supplier_score=supplier,
        discipline_score=discipline,
    )
    assert compute_health_score(components) == pytest.approx(esperado, abs=0.001)


def test_score_is_deterministic():
    """Mismos inputs → mismo score en 1000 ejecuciones (compat shim)."""
    config = _make_config()
    results = set()
    for _ in range(1000):
        components = ComponentScores(
            cash_score=compute_cash_score(15.0, config),
            stock_score=compute_stock_score(0, 2, 50),
            supplier_score=compute_supplier_score(3, 0),
            discipline_score=compute_discipline_score(6, 7),
        )
        score = compute_health_score(components)
        results.add(round(score, 6))
    assert len(results) == 1


# ── Tests de componentes individuales ─────────────────────────────────────────


def test_cash_component_critical():
    config = _make_config(healthy_days_min=10.0, warning_days_min=7.0)
    assert compute_cash_score(2.0, config) < 30.0


def test_cash_component_healthy():
    config = _make_config(healthy_days_min=10.0, warning_days_min=7.0)
    assert compute_cash_score(25.0, config) == 100.0


def test_cash_component_warning_zone():
    config = _make_config(healthy_days_min=10.0, warning_days_min=7.0)
    score = compute_cash_score(8.0, config)
    assert 30.0 <= score < 70.0


def test_cash_component_healthy_zone():
    config = _make_config(healthy_days_min=10.0, warning_days_min=7.0)
    score = compute_cash_score(12.0, config)
    assert 70.0 <= score < 100.0


@pytest.mark.parametrize(
    ("stockout_count", "slow_moving_count", "total_products", "esperado"),
    [
        pytest.param(0, 0, 50, 100.0, id="test_stock_score_no_stockouts"),
        pytest.param(3, 0, 50, 70.0, id="test_stock_score_with_stockouts"),
        # sin catálogo no hay nada que puntuar → neutral, no 0
        pytest.param(0, 0, 0, 50.0, id="test_stock_score_no_products"),
    ],
)
def test_stock_score(stockout_count, slow_moving_count, total_products, esperado):
    assert compute_stock_score(stockout_count, slow_moving_count, total_products) == pytest.approx(
        esperado, abs=0.001
    )


@pytest.mark.parametrize(
    ("active_suppliers", "overdue_orders", "esperado"),
    [
        pytest.param(3, 0, 100.0, id="test_supplier_score_all_ok"),
        pytest.param(3, 2, 70.0, id="test_supplier_score_overdue"),
        # sin proveedores cargados → neutral, no 0
        pytest.param(0, 0, 50.0, id="test_supplier_score_no_suppliers"),
    ],
)
def test_supplier_score(active_suppliers, overdue_orders, esperado):
    assert compute_supplier_score(active_suppliers, overdue_orders) == pytest.approx(
        esperado, abs=0.001
    )


@pytest.mark.parametrize(
    ("days_with_data", "total_days", "esperado", "tolerancia"),
    [
        pytest.param(7, 7, 100.0, 0.001, id="test_discipline_score_full"),
        pytest.param(6, 7, 85.71, 0.1, id="test_discipline_score_partial"),
        # sin días de referencia la disciplina es 0, no neutral
        pytest.param(0, 0, 0.0, 0.001, id="test_discipline_score_no_days"),
    ],
)
def test_discipline_score(days_with_data, total_days, esperado, tolerancia):
    assert compute_discipline_score(days_with_data, total_days) == pytest.approx(
        esperado, abs=tolerancia
    )


# ── Tests de alertas v2 (_build_alerts con ComponentScoresV2) ────────────────


def test_alerts_are_top3():
    """_build_alerts siempre retorna máximo 3 alertas."""
    from app.application.agents.health.agent import AgentHealth

    agent = AgentHealth()
    scores = _make_scores_v2(cash=10, stock=20, supplier=100, margin=30, growth=20)
    alerts = agent._build_alerts(scores)
    assert len(alerts) <= 3


def test_alerts_cash_critical():
    """cash_score < 30 → alerta CRITICAL."""
    from app.application.agents.health.agent import AgentHealth

    agent = AgentHealth()
    scores = _make_scores_v2(cash=10, stock=100, supplier=100, margin=100, growth=100)
    alerts = agent._build_alerts(scores)
    assert any(a["type"] == "CRITICAL" for a in alerts)


def test_alerts_cash_warning():
    """cash_score entre 30 y 60 → alerta WARNING (no CRITICAL)."""
    from app.application.agents.health.agent import AgentHealth

    agent = AgentHealth()
    scores = _make_scores_v2(cash=45, stock=100, supplier=100, margin=100, growth=100)
    alerts = agent._build_alerts(scores)
    types = [a["type"] for a in alerts]
    assert "WARNING" in types
    assert "CRITICAL" not in types


def test_alerts_growth_low():
    """growth_score < 40 → alerta INFO."""
    from app.application.agents.health.agent import AgentHealth

    agent = AgentHealth()
    scores = _make_scores_v2(cash=80, stock=80, supplier=80, margin=80, growth=30)
    alerts = agent._build_alerts(scores)
    components = [a["component"] for a in alerts]
    assert "growth" in components


# ── Tests del cálculo — LLM no debe ser llamado ───────────────────────────────


def test_llm_not_called_for_score():
    """El cálculo del score NO llama al cliente Anthropic."""
    config = _make_config()
    mock_client = MagicMock()
    components = ComponentScores(
        cash_score=compute_cash_score(15.0, config),
        stock_score=compute_stock_score(0, 2, 50),
        supplier_score=compute_supplier_score(3, 0),
        discipline_score=compute_discipline_score(6, 7),
    )
    score = compute_health_score(components)
    mock_client.messages.create.assert_not_called()
    assert 0.0 <= score <= 100.0


# ── Tests de process() ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_process_no_db_returns_error():
    """Sin DB → status=error."""
    from app.application.agents.health.agent import AgentHealth

    agent = AgentHealth()  # sin DB
    result = await agent.process(_make_request())
    assert result.status == "error"


@pytest.mark.asyncio
async def test_process_low_confidence_returns_clarification():
    """BusinessState con confidence=LOW → requires_clarification, sin LLM."""
    from app.application.agents.health.agent import AgentHealth

    agent = AgentHealth(db=MagicMock())
    mock_state = MagicMock()
    mock_state.confidence_level = "LOW"
    mock_state.data_completeness_score = 30.0
    mock_client = _mock_anthropic_client()
    agent.client = mock_client

    with (
        patch.object(
            agent,
            "_load_business_meta",
            new=AsyncMock(return_value=("Test", Vertical.KIOSCO_ALMACEN)),
        ),
        patch(
            "app.application.agents.health.agent.collect", new=AsyncMock(return_value=mock_state)
        ),
    ):
        result = await agent.process(_make_request())

    assert result.status == "requires_clarification"
    assert result.confidence == "LOW"
    mock_client.messages.create.assert_not_called()


@pytest.mark.asyncio
async def test_el_gate_mira_el_benchmark_que_realmente_puntua():
    """Datos impecables medidos contra una vara sin fundamento → empty state.

    El agente resolvía la confianza de la vara leyendo el JSON del RUBRO, pero
    después puntuaba con el override del tenant. Mientras las dos procedencias
    dieran HIGH la diferencia no se veía; acá el override es lo único con
    confianza baja, así que mirar el rubro devolvería HIGH y dejaría pasar un
    diagnóstico que se calcula contra un umbral que nadie fundamentó.
    """
    from app.application.agents.health.agent import AgentHealth
    from app.heuristics.verticals import BenchmarkProvenance, MarginBenchmark

    vara_sin_fundamento = MarginBenchmark(
        critical_below=0.04,
        warning_below=0.08,
        healthy_min=0.08,
        healthy_max=0.14,
        provenance=BenchmarkProvenance.DATA_DRIVEN,
    )
    assert vara_sin_fundamento.confidence == "LOW", "premisa del test"

    agent = AgentHealth(db=MagicMock())
    mock_client = _mock_anthropic_client()
    agent.client = mock_client

    mock_state = MagicMock()
    mock_state.confidence_level = "HIGH"  # los DATOS del negocio están completos
    mock_state.data_completeness_score = 90.0
    mock_state.vertical_code = Vertical.KIOSCO_ALMACEN.value

    with (
        patch.object(
            agent,
            "_load_business_meta",
            new=AsyncMock(return_value=("Test", Vertical.KIOSCO_ALMACEN)),
        ),
        patch(
            "app.application.agents.health.agent.collect", new=AsyncMock(return_value=mock_state)
        ),
        patch(
            "app.application.agents.health.agent.get_margin_benchmark",
            new=AsyncMock(return_value=vara_sin_fundamento),
        ),
    ):
        result = await agent.process(_make_request(business_id=str(uuid.uuid4())))

    assert result.status == "requires_clarification"
    mock_client.messages.create.assert_not_called()


@pytest.mark.asyncio
async def test_el_benchmark_del_gate_es_el_mismo_que_recibe_el_calculo():
    """Una sola resolución del benchmark, compartida por el gate y el score.

    Si `compute_scores` recibiera `None`, volvería a caer al JSON del rubro por su
    cuenta: serían dos resoluciones del mismo concepto, libres de divergir sin que
    nada las compare.
    """
    from app.application.agents.health.agent import AgentHealth
    from app.heuristics.verticals import BenchmarkProvenance, MarginBenchmark

    override_del_tenant = MarginBenchmark(
        critical_below=0.10,
        warning_below=0.20,
        healthy_min=0.20,
        healthy_max=0.35,
        provenance=BenchmarkProvenance.TENANT_OVERRIDE,
    )

    agent = AgentHealth(db=MagicMock())
    agent.client = _mock_anthropic_client()

    mock_state = MagicMock()
    mock_state.confidence_level = "HIGH"
    mock_state.data_completeness_score = 90.0
    mock_state.vertical_code = Vertical.KIOSCO_ALMACEN.value

    with (
        patch("app.application.agents.health.agent.EventBus.emit"),
        patch.object(
            agent,
            "_load_business_meta",
            new=AsyncMock(return_value=("Test", Vertical.KIOSCO_ALMACEN)),
        ),
        patch(
            "app.application.agents.health.agent.collect", new=AsyncMock(return_value=mock_state)
        ),
        patch(
            "app.application.agents.health.agent.get_margin_benchmark",
            new=AsyncMock(return_value=override_del_tenant),
        ),
        patch(
            "app.application.agents.health.agent.compute_scores",
            return_value=_make_scores_v2(total=72, confidence_level="HIGH", completeness=90.0),
        ) as mock_compute,
    ):
        await agent.process(_make_request(business_id=str(uuid.uuid4())))

    assert mock_compute.call_args.kwargs["benchmark"] is override_del_tenant


@pytest.mark.asyncio
async def test_sin_override_el_calculo_recibe_el_benchmark_del_rubro():
    """Contrapeso: sin override, la vara resuelta es la del JSON del rubro.

    Nunca `None`. Pasar `None` volvería a delegar la resolución río abajo, que es
    justamente la duplicación que los dos tests de arriba existen para cerrar.
    """
    from app.application.agents.health.agent import AgentHealth
    from app.heuristics.verticals.loader import load_vertical_heuristics

    agent = AgentHealth(db=MagicMock())
    agent.client = _mock_anthropic_client()

    mock_state = MagicMock()
    mock_state.confidence_level = "HIGH"
    mock_state.data_completeness_score = 90.0
    mock_state.vertical_code = Vertical.KIOSCO_ALMACEN.value

    with (
        patch("app.application.agents.health.agent.EventBus.emit"),
        patch.object(
            agent,
            "_load_business_meta",
            new=AsyncMock(return_value=("Test", Vertical.KIOSCO_ALMACEN)),
        ),
        patch(
            "app.application.agents.health.agent.collect", new=AsyncMock(return_value=mock_state)
        ),
        patch(
            "app.application.agents.health.agent.get_margin_benchmark",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "app.application.agents.health.agent.compute_scores",
            return_value=_make_scores_v2(total=72, confidence_level="HIGH", completeness=90.0),
        ) as mock_compute,
    ):
        await agent.process(_make_request(business_id=str(uuid.uuid4())))

    esperado = load_vertical_heuristics(Vertical.KIOSCO_ALMACEN).margin
    assert mock_compute.call_args.kwargs["benchmark"] == esperado


@pytest.mark.asyncio
async def test_process_high_confidence_returns_success():
    """BusinessState con confidence=HIGH → status=success con narrativa LLM."""
    from app.application.agents.health.agent import AgentHealth

    agent = AgentHealth(db=MagicMock())
    mock_client = _mock_anthropic_client("Narrativa ejecutiva de prueba.")
    agent.client = mock_client

    mock_state = MagicMock()
    mock_state.confidence_level = "HIGH"
    mock_state.data_completeness_score = 90.0
    mock_state.vertical_code = Vertical.KIOSCO_ALMACEN.value

    high_scores = _make_scores_v2(total=72, confidence_level="HIGH", completeness=90.0)

    with (
        patch("app.application.agents.health.agent.EventBus.emit"),
        patch.object(
            agent,
            "_load_business_meta",
            new=AsyncMock(return_value=("Test", Vertical.KIOSCO_ALMACEN)),
        ),
        patch(
            "app.application.agents.health.agent.collect", new=AsyncMock(return_value=mock_state)
        ),
        patch("app.application.agents.health.agent.compute_scores", return_value=high_scores),
    ):
        result = await agent.process(_make_request())

    assert result.status == "success"
    assert result.result["health_score"] == 72
    assert "components" in result.result
    assert result.result["formula_version"] == "v2"
    mock_client.messages.create.assert_called_once()


@pytest.mark.asyncio
async def test_process_emits_event_on_success():
    """EventBus.emit se llama cuando el score se calcula con éxito."""
    from app.application.agents.health.agent import AgentHealth

    agent = AgentHealth(db=MagicMock())
    mock_client = _mock_anthropic_client("Narrativa.")
    agent.client = mock_client

    mock_state = MagicMock()
    mock_state.confidence_level = "HIGH"
    mock_state.data_completeness_score = 90.0
    mock_state.vertical_code = Vertical.KIOSCO_ALMACEN.value
    high_scores = _make_scores_v2(total=72)

    with (
        patch("app.application.agents.health.agent.EventBus.emit") as mock_emit,
        patch.object(
            agent,
            "_load_business_meta",
            new=AsyncMock(return_value=("Test", Vertical.KIOSCO_ALMACEN)),
        ),
        patch(
            "app.application.agents.health.agent.collect", new=AsyncMock(return_value=mock_state)
        ),
        patch("app.application.agents.health.agent.compute_scores", return_value=high_scores),
    ):
        await agent.process(_make_request())

    mock_emit.assert_called_once()
    assert mock_emit.call_args[0][0] == "HEALTH_SCORE_UPDATED"
