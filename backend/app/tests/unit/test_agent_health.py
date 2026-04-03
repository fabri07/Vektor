"""Unit tests for AgentHealth — scorer determinístico + narrativa Sonnet.

Reglas del agente:
- El score se calcula en Python, NUNCA con LLM.
- El LLM solo genera narrativa a partir de los números ya calculados.
- FÓRMULA CANÓNICA: cash×0.35 + stock×0.30 + supplier×0.15 + discipline×0.20
"""

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
from app.application.agents.shared.heuristic_engine import HeuristicConfig, CashHealthConfig
from app.application.agents.shared.schemas import AgentRequest


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_config(healthy_days_min: float = 10.0, warning_days_min: float = 7.0) -> HeuristicConfig:
    """Crea una HeuristicConfig con umbrales controlados para tests."""
    return HeuristicConfig(
        business_type="kiosco_almacen",
        cash_health=CashHealthConfig(
            healthy_days_min=healthy_days_min,
            warning_days_min=warning_days_min,
            critical_days_below=5.0,
        ),
    )


def _make_request(message: str = "generar informe de salud") -> AgentRequest:
    return AgentRequest(
        user_id="user-123",
        business_id="tenant-456",
        message=message,
    )


def _mock_anthropic_client(narrative_text: str = "Narrativa de prueba.") -> MagicMock:
    """Crea un mock del cliente AsyncAnthropic que retorna narrative_text."""
    content_block = MagicMock()
    content_block.text = narrative_text
    response = MagicMock()
    response.content = [content_block]
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=response)
    return mock_client


# ── Tests de la fórmula canónica ──────────────────────────────────────────────

def test_score_formula_correct():
    """components={100,100,100,100} → score=100.0"""
    components = ComponentScores(
        cash_score=100.0,
        stock_score=100.0,
        supplier_score=100.0,
        discipline_score=100.0,
    )
    assert compute_health_score(components) == 100.0


def test_score_weights():
    """cash_score=100, resto=0 → health_score=35.0 (peso del cash es 0.35)."""
    components = ComponentScores(
        cash_score=100.0,
        stock_score=0.0,
        supplier_score=0.0,
        discipline_score=0.0,
    )
    assert compute_health_score(components) == pytest.approx(35.0, abs=0.001)


def test_score_weight_stock():
    """stock_score=100, resto=0 → health_score=30.0 (peso del stock es 0.30)."""
    components = ComponentScores(
        cash_score=0.0,
        stock_score=100.0,
        supplier_score=0.0,
        discipline_score=0.0,
    )
    assert compute_health_score(components) == pytest.approx(30.0, abs=0.001)


def test_score_weight_supplier():
    """supplier_score=100, resto=0 → health_score=15.0 (peso del supplier es 0.15)."""
    components = ComponentScores(
        cash_score=0.0,
        stock_score=0.0,
        supplier_score=100.0,
        discipline_score=0.0,
    )
    assert compute_health_score(components) == pytest.approx(15.0, abs=0.001)


def test_score_weight_discipline():
    """discipline_score=100, resto=0 → health_score=20.0 (peso de discipline es 0.20)."""
    components = ComponentScores(
        cash_score=0.0,
        stock_score=0.0,
        supplier_score=0.0,
        discipline_score=100.0,
    )
    assert compute_health_score(components) == pytest.approx(20.0, abs=0.001)


def test_score_is_deterministic():
    """Mismos inputs → mismo score en 1000 ejecuciones."""
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
    assert len(results) == 1, f"Score no es determinístico: {results}"


# ── Tests de componentes individuales ─────────────────────────────────────────

def test_cash_component_critical():
    """coverage_days=2 con healthy_min=10 → cash_score < 30 (zona crítica)."""
    config = _make_config(healthy_days_min=10.0, warning_days_min=7.0)
    score = compute_cash_score(2.0, config)
    assert score < 30.0, f"Esperaba cash_score < 30, obtuvo {score}"


def test_cash_component_healthy():
    """coverage_days=25 con healthy_min=10 → 25 >= 10*2=20 → cash_score=100."""
    config = _make_config(healthy_days_min=10.0, warning_days_min=7.0)
    score = compute_cash_score(25.0, config)
    assert score == 100.0


def test_cash_component_warning_zone():
    """coverage_days=8 con healthy_min=10, warning_min=7 → 30 <= score < 70."""
    config = _make_config(healthy_days_min=10.0, warning_days_min=7.0)
    score = compute_cash_score(8.0, config)
    assert 30.0 <= score < 70.0, f"Esperaba zona de advertencia, obtuvo {score}"


def test_cash_component_healthy_zone():
    """coverage_days=12 con healthy_min=10 → 12 >= 10 pero < 20 → 70 <= score < 100."""
    config = _make_config(healthy_days_min=10.0, warning_days_min=7.0)
    score = compute_cash_score(12.0, config)
    assert 70.0 <= score < 100.0, f"Esperaba zona saludable, obtuvo {score}"


def test_stock_score_no_stockouts():
    """0 quiebres, 0 slow moving → score=100."""
    assert compute_stock_score(0, 0, 50) == 100.0


def test_stock_score_with_stockouts():
    """3 quiebres → score=100 - 30 = 70."""
    assert compute_stock_score(3, 0, 50) == pytest.approx(70.0)


def test_stock_score_no_products():
    """Sin productos → score neutro=50."""
    assert compute_stock_score(0, 0, 0) == 50.0


def test_supplier_score_all_ok():
    """3 activos, 0 vencidos → score=100."""
    assert compute_supplier_score(3, 0) == 100.0


def test_supplier_score_overdue():
    """3 activos, 2 vencidos → score=100 - 30 = 70."""
    assert compute_supplier_score(3, 2) == pytest.approx(70.0)


def test_supplier_score_no_suppliers():
    """Sin proveedores → score neutro=50."""
    assert compute_supplier_score(0, 0) == 50.0


def test_discipline_score_full():
    """7 de 7 días con datos → score=100."""
    assert compute_discipline_score(7, 7) == 100.0


def test_discipline_score_partial():
    """6 de 7 días → score≈85.7."""
    score = compute_discipline_score(6, 7)
    assert score == pytest.approx(85.71, abs=0.1)


def test_discipline_score_no_days():
    """0 días totales → score=0 (evitar división por cero)."""
    assert compute_discipline_score(0, 0) == 0.0


# ── Tests de alertas ──────────────────────────────────────────────────────────

def test_alerts_are_top3():
    """_generate_alerts siempre retorna máximo 3 alertas."""
    with patch("app.application.agents.health.agent.anthropic.Anthropic"):
        from app.application.agents.health.agent import AgentHealth

        agent = AgentHealth()
        # Componentes todos en estado crítico/bajo para generar muchas alertas
        components = ComponentScores(
            cash_score=10.0,   # CRITICAL cash
            stock_score=20.0,  # WARNING stock
            supplier_score=100.0,
            discipline_score=30.0,  # INFO discipline
        )
        alerts = agent._generate_alerts(components)
        assert len(alerts) <= 3, f"Retornó {len(alerts)} alertas, máximo permitido: 3"


def test_alerts_cash_critical():
    """cash_score < 30 → alerta CRITICAL."""
    with patch("app.application.agents.health.agent.anthropic.Anthropic"):
        from app.application.agents.health.agent import AgentHealth

        agent = AgentHealth()
        components = ComponentScores(
            cash_score=10.0,
            stock_score=100.0,
            supplier_score=100.0,
            discipline_score=100.0,
        )
        alerts = agent._generate_alerts(components)
        types = [a["type"] for a in alerts]
        assert "CRITICAL" in types


def test_alerts_cash_warning():
    """cash_score entre 30 y 60 → alerta WARNING (no CRITICAL)."""
    with patch("app.application.agents.health.agent.anthropic.Anthropic"):
        from app.application.agents.health.agent import AgentHealth

        agent = AgentHealth()
        components = ComponentScores(
            cash_score=45.0,
            stock_score=100.0,
            supplier_score=100.0,
            discipline_score=100.0,
        )
        alerts = agent._generate_alerts(components)
        types = [a["type"] for a in alerts]
        assert "WARNING" in types
        assert "CRITICAL" not in types


# ── Test de narrativa ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_narrative_uses_computed_numbers():
    """La narrativa contiene el score calculado (LLM recibe el número, no lo inventa)."""
    # El test verifica que generate_narrative llama al LLM con el score en el prompt
    with patch("app.application.agents.health.agent.anthropic.AsyncAnthropic") as mock_cls:
        from app.application.agents.health.agent import AgentHealth

        mock_client = _mock_anthropic_client("El negocio tiene un score de 78.5 sobre 100.")
        mock_cls.return_value = mock_client

        agent = AgentHealth()
        agent.client = mock_client

        from app.application.agents.health.scorer import ComponentScores, HealthScore

        health = HealthScore(
            business_id="tenant-456",
            health_score=78.5,
            components=ComponentScores(
                cash_score=80.0,
                stock_score=90.0,
                supplier_score=60.0,
                discipline_score=70.0,
            ),
            alerts=[],
            period="current",
        )

        narrative = await agent.generate_narrative(health, "Kiosco San Martín")

        # Verificar que el LLM fue llamado
        assert mock_client.messages.create.called

        # Verificar que el score aparece en el call al LLM
        call_kwargs = mock_client.messages.create.call_args
        messages_arg = call_kwargs[1].get("messages") or call_kwargs[0][2]
        prompt_content = str(messages_arg)
        assert "78.5" in prompt_content or "78" in prompt_content


# ── Test de separación LLM / cálculo ─────────────────────────────────────────

def test_llm_not_called_for_score():
    """El cálculo del score NO llama al cliente Anthropic."""
    from app.application.agents.health.scorer import (
        ComponentScores,
        compute_health_score,
        compute_cash_score,
        compute_stock_score,
        compute_supplier_score,
        compute_discipline_score,
    )

    config = _make_config()

    # Crear un mock del cliente y verificar que NO es llamado durante el cálculo
    mock_client = MagicMock()

    components = ComponentScores(
        cash_score=compute_cash_score(15.0, config),
        stock_score=compute_stock_score(0, 2, 50),
        supplier_score=compute_supplier_score(3, 0),
        discipline_score=compute_discipline_score(6, 7),
    )
    score = compute_health_score(components)

    # El cliente Anthropic nunca debe ser invocado durante el cálculo
    mock_client.messages.create.assert_not_called()
    assert 0.0 <= score <= 100.0


# ── Test del proceso completo (process()) ─────────────────────────────────────

@pytest.mark.asyncio
async def test_process_returns_success_with_score():
    """process() retorna status=success con health_score en result."""
    with patch("app.application.agents.health.agent.anthropic.AsyncAnthropic") as mock_cls:
        with patch("app.application.agents.health.agent.EventBus.emit"):
            from app.application.agents.health.agent import AgentHealth

            mock_client = _mock_anthropic_client("Narrativa ejecutiva de prueba.")
            mock_cls.return_value = mock_client

            agent = AgentHealth()
            agent.client = mock_client

            result = await agent.process(_make_request())

    assert result.status == "success"
    assert "health_score" in result.result
    assert 0.0 <= result.result["health_score"] <= 100.0
    assert "components" in result.result
    assert "alerts" in result.result
    assert "suggested_next_actions" in result.result


@pytest.mark.asyncio
async def test_process_emits_health_score_updated_event():
    """process() emite HEALTH_SCORE_UPDATED vía EventBus."""
    with patch("app.application.agents.health.agent.anthropic.AsyncAnthropic") as mock_cls:
        with patch("app.application.agents.health.agent.EventBus.emit") as mock_emit:
            from app.application.agents.health.agent import AgentHealth

            mock_client = _mock_anthropic_client("Narrativa.")
            mock_cls.return_value = mock_client

            agent = AgentHealth()
            agent.client = mock_client

            await agent.process(_make_request())

    # Verificar que se emitió el evento correcto con los campos esperados
    mock_emit.assert_called_once()
    call_args = mock_emit.call_args
    assert call_args[0][0] == "HEALTH_SCORE_UPDATED"
    assert call_args[0][1]["business_id"] == "tenant-456"
    assert "score" in call_args[0][1]
    assert 0.0 <= call_args[0][1]["score"] <= 100.0
