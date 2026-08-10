"""Tests unitarios para AgentMarketing (Véktor v4 — Fase 4).

Cubre (Brief F4):
- agent_name == "agent_marketing"
- process() con ANALYZE_MARKETING_DATA + analizar_marketing → message con cifras del service
- Sin datos (has_data=False) → mensaje no-invention, confidence MEDIUM, sin cifras
- sugerir_campana con clientes → top clientes en el mensaje
- sugerir_campana sin ventas → mensaje no-invention
- ROI ads: foco gasto vs ingresos (ratio correcto)
- ROI ads sin gasto en ads → mensaje claro, sin dividir por cero
- ROI ads sin ventas pero con ads → mensaje claro, sin dividir por cero
- Cross-tenant: dashboard de un tenant no ve métricas de otro (has_data=False)
- Routing: INTENT_TO_ACTION_TYPE["analizar_marketing"] == ANALYZE_MARKETING_DATA
- Routing: INTENT_TO_AGENT["analizar_marketing"] == "agent_marketing"
- registry.get_sub_agent("agent_marketing") → instancia de AgentMarketing
- is_valid_action_type("ANALYZE_MARKETING_DATA") → True
"""

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.application.agents.shared.schemas import (
    ActionType,
    AgentRequest,
    AgentTask,
    Confidence,
)

_TENANT_A = "00000000-0000-0000-0000-000000000001"
_TENANT_B = "00000000-0000-0000-0000-000000000002"
_USER = "00000000-0000-0000-0000-000000000099"


def _req(tenant_id: str = _TENANT_A) -> AgentRequest:
    return AgentRequest(
        user_id=_USER, business_id=tenant_id, message="cómo anduvo mi marketing"
    )


def _task(intent: str) -> AgentTask:
    return AgentTask(
        agent="agent_marketing",
        action_type=ActionType.ANALYZE_MARKETING_DATA,
        entities={"_intent": intent},
    )


def _mock_dashboard(has_data: bool = True, ratio: float | None = 0.05):
    """Construye un mock de MarketingDashboardResponse con datos fijos."""
    from app.schemas.marketing import AdsVsSales, MarketingDashboardResponse, PlatformDashboard

    today = date.today()
    platforms = (
        [
            PlatformDashboard(
                platform="instagram",
                followers=1000,
                reach=5000,
                engagement=200,
                ads_spend_ars=Decimal("2000.00"),
            )
        ]
        if has_data
        else []
    )
    total_ads = Decimal("2000.00") if has_data else Decimal("0")
    revenue = Decimal("40000.00") if has_data else Decimal("0")

    return MarketingDashboardResponse(
        days=30,
        from_date=today,
        to_date=today,
        has_data=has_data,
        platforms=platforms,
        total_followers=1000 if has_data else 0,
        total_reach=5000 if has_data else 0,
        total_engagement=200 if has_data else 0,
        total_ads_spend_ars=total_ads,
        ads_vs_sales=AdsVsSales(
            ads_spend_ars=total_ads,
            revenue_ars=revenue,
            ratio=ratio if has_data else None,
        ),
    )


# ── agent_name ────────────────────────────────────────────────────────────────


def test_agent_marketing_name():
    from app.application.agents.marketing.agent import AgentMarketing

    assert AgentMarketing().agent_name == "agent_marketing"


# ── Routing ───────────────────────────────────────────────────────────────────


def test_routing_analizar_marketing_to_agent_marketing():
    from app.application.agents.ceo.team_plan_builder import INTENT_TO_AGENT

    assert INTENT_TO_AGENT["analizar_marketing"] == "agent_marketing"


def test_routing_analizar_marketing_action_type():
    from app.application.agents.ceo.team_plan_builder import INTENT_TO_ACTION_TYPE

    assert INTENT_TO_ACTION_TYPE["analizar_marketing"] == ActionType.ANALYZE_MARKETING_DATA


def test_is_valid_action_type_analyze_marketing_data():
    from app.application.security.prompt_defense import is_valid_action_type

    assert is_valid_action_type("ANALYZE_MARKETING_DATA") is True


def test_registry_agent_marketing_returns_instance():
    from app.application.agents.marketing.agent import AgentMarketing
    from app.application.agents.registry import get_sub_agent

    agent = get_sub_agent("agent_marketing", db=None)
    assert isinstance(agent, AgentMarketing)


# ── Sin DB → mensaje claro, no reventar ──────────────────────────────────────


async def test_sin_db_devuelve_mensaje_claro():
    from app.application.agents.marketing.agent import AgentMarketing

    agent = AgentMarketing(db=None)
    resp = await agent.process(_req(), task=_task("analizar_marketing"))

    assert resp.status == "success"
    assert resp.result["summary"] == "marketing_sin_datos"
    assert resp.confidence == Confidence.MEDIUM


# ── Dashboard con datos → cifras del service ─────────────────────────────────


async def test_dashboard_con_datos_contiene_cifras():
    """Con has_data=True → message contiene seguidores/ads y summary correcto."""
    from app.application.agents.marketing.agent import AgentMarketing

    mock_db = MagicMock()
    agent = AgentMarketing(db=mock_db)
    dashboard = _mock_dashboard(has_data=True, ratio=0.05)

    with patch(
        "app.application.services.marketing_service.MarketingService.get_dashboard",
        new_callable=AsyncMock,
        return_value=dashboard,
    ):
        resp = await agent.process(_req(), task=_task("analizar_marketing"))

    assert resp.status == "success"
    assert resp.result["summary"] == "analizar_marketing"
    # Cifras del service presentes en el mensaje
    assert "1.000" in resp.message or "1000" in resp.message  # seguidores
    assert "2.000" in resp.message or "2000" in resp.message  # ads spend
    # Structured data tiene las claves esperadas
    structured = resp.result["structured_data"]
    assert structured["total_followers"] == 1000
    assert structured["total_ads_spend_ars"] == 2000.0
    assert structured["ratio"] == pytest.approx(0.05)


# ── has_data=False → no-invention ────────────────────────────────────────────


async def test_dashboard_sin_datos_no_inventa():
    """has_data=False → 'todavía no hay datos', confidence MEDIUM, sin cifras."""
    from app.application.agents.marketing.agent import AgentMarketing

    mock_db = MagicMock()
    agent = AgentMarketing(db=mock_db)
    dashboard = _mock_dashboard(has_data=False)

    with patch(
        "app.application.services.marketing_service.MarketingService.get_dashboard",
        new_callable=AsyncMock,
        return_value=dashboard,
    ):
        resp = await agent.process(_req(), task=_task("analizar_marketing"))

    assert resp.status == "success"
    assert resp.result["summary"] == "marketing_sin_datos"
    assert resp.confidence == Confidence.MEDIUM
    # No-invention: no debe mostrar cifras
    assert "$" not in resp.message


# ── sugerir_campana con clientes → top clientes ──────────────────────────────


async def test_sugerir_campana_con_clientes():
    """sugerir_campana con ventas → top clientes en el mensaje."""
    from app.application.agents.marketing.agent import AgentMarketing

    mock_db = MagicMock()
    agent = AgentMarketing(db=mock_db)

    sales_data = [
        {"customer_id": "a", "customer_name": "Ana", "total": 5000.0, "n_sales": 5},
        {"customer_id": "b", "customer_name": "Beto", "total": 12000.0, "n_sales": 3},
    ]

    with patch(
        "app.persistence.repositories.transaction_repository.SaleRepository"
    ) as MockSR:
        sr_instance = AsyncMock()
        sr_instance.get_sales_by_customer = AsyncMock(return_value=sales_data)
        MockSR.return_value = sr_instance

        resp = await agent.process(_req(), task=_task("sugerir_campana"))

    assert resp.status == "success"
    assert resp.result["summary"] == "sugerir_campana"
    # Los dos clientes deben aparecer en el mensaje
    assert "Beto" in resp.message
    assert "Ana" in resp.message
    # Structured data
    top = resp.result["structured_data"]["top"]
    # Beto tiene mayor total (12.000) → primero en el ranking
    assert top[0]["customer_name"] == "Beto"


# ── sugerir_campana sin ventas → no-invention ────────────────────────────────


async def test_sugerir_campana_sin_clientes_no_inventa():
    """Sin ventas con clientes → mensaje no-invention, no muestra top."""
    from app.application.agents.marketing.agent import AgentMarketing

    mock_db = MagicMock()
    agent = AgentMarketing(db=mock_db)

    with patch(
        "app.persistence.repositories.transaction_repository.SaleRepository"
    ) as MockSR:
        sr_instance = AsyncMock()
        sr_instance.get_sales_by_customer = AsyncMock(return_value=[])
        MockSR.return_value = sr_instance

        resp = await agent.process(_req(), task=_task("sugerir_campana"))

    assert resp.status == "success"
    assert resp.result["summary"] == "sugerir_campana_sin_clientes"
    assert resp.confidence == Confidence.MEDIUM
    assert "$" not in resp.message


# ── ROI ads con datos completos ───────────────────────────────────────────────


async def test_roi_ads_con_datos():
    """analizar_roi_ads con ads y ventas → ratio en el mensaje."""
    from app.application.agents.marketing.agent import AgentMarketing

    mock_db = MagicMock()
    agent = AgentMarketing(db=mock_db)
    dashboard = _mock_dashboard(has_data=True, ratio=0.05)

    with patch(
        "app.application.services.marketing_service.MarketingService.get_dashboard",
        new_callable=AsyncMock,
        return_value=dashboard,
    ):
        resp = await agent.process(_req(), task=_task("analizar_roi_ads"))

    assert resp.status == "success"
    assert resp.result["summary"] == "analizar_roi_ads"
    assert "5.0%" in resp.message or "5,0%" in resp.message or "5.0" in resp.message
    structured = resp.result["structured_data"]
    assert structured["ads_spend_ars"] == pytest.approx(2000.0)
    assert structured["revenue_ars"] == pytest.approx(40000.0)


# ── ROI ads sin gasto en ads → no dividir por cero ───────────────────────────


async def test_roi_ads_sin_gasto_mensaje_claro():
    """ROI ads sin gasto en ads → mensaje claro, sin dividir por cero."""
    from app.application.agents.marketing.agent import AgentMarketing
    from app.schemas.marketing import AdsVsSales, MarketingDashboardResponse

    mock_db = MagicMock()
    agent = AgentMarketing(db=mock_db)

    today = date.today()
    dashboard_no_ads = MarketingDashboardResponse(
        days=30,
        from_date=today,
        to_date=today,
        has_data=True,  # hay métricas de seguidores pero sin gasto en ads
        platforms=[],
        total_followers=500,
        total_reach=1000,
        total_engagement=100,
        total_ads_spend_ars=Decimal("0"),
        ads_vs_sales=AdsVsSales(
            ads_spend_ars=Decimal("0"),
            revenue_ars=Decimal("10000"),
            ratio=None,
        ),
    )

    with patch(
        "app.application.services.marketing_service.MarketingService.get_dashboard",
        new_callable=AsyncMock,
        return_value=dashboard_no_ads,
    ):
        resp = await agent.process(_req(), task=_task("analizar_roi_ads"))

    assert resp.status == "success"
    assert resp.result["summary"] == "roi_ads_sin_gasto"
    assert resp.confidence == Confidence.MEDIUM


# ── ROI ads con ads pero sin ventas → no dividir por cero ────────────────────


async def test_roi_ads_sin_ventas_mensaje_claro():
    """ROI ads: con gasto en ads pero sin ventas → mensaje sin dividir por cero."""
    from app.application.agents.marketing.agent import AgentMarketing
    from app.schemas.marketing import AdsVsSales, MarketingDashboardResponse, PlatformDashboard

    mock_db = MagicMock()
    agent = AgentMarketing(db=mock_db)

    today = date.today()
    dashboard_no_revenue = MarketingDashboardResponse(
        days=30,
        from_date=today,
        to_date=today,
        has_data=True,
        platforms=[
            PlatformDashboard(
                platform="instagram",
                followers=800,
                reach=2000,
                engagement=100,
                ads_spend_ars=Decimal("1500.00"),
            )
        ],
        total_followers=800,
        total_reach=2000,
        total_engagement=100,
        total_ads_spend_ars=Decimal("1500.00"),
        ads_vs_sales=AdsVsSales(
            ads_spend_ars=Decimal("1500.00"),
            revenue_ars=Decimal("0"),
            ratio=None,  # revenue==0 → ratio=None en el service
        ),
    )

    with patch(
        "app.application.services.marketing_service.MarketingService.get_dashboard",
        new_callable=AsyncMock,
        return_value=dashboard_no_revenue,
    ):
        resp = await agent.process(_req(), task=_task("analizar_roi_ads"))

    assert resp.status == "success"
    assert resp.result["summary"] == "roi_ads_sin_ventas"
    assert resp.confidence == Confidence.MEDIUM
    assert "1.500" in resp.message or "1500" in resp.message


# ── Cross-tenant: tenant B no ve métricas de tenant A ────────────────────────


async def test_cross_tenant_no_ve_metricas_ajenas():
    """Dashboard de tenant B devuelve has_data=False (no ve métricas de tenant A)."""
    from app.application.agents.marketing.agent import AgentMarketing

    mock_db = MagicMock()
    agent = AgentMarketing(db=mock_db)

    # Tenant A tiene datos; tenant B no tiene ningún dato
    tenant_b_dashboard = _mock_dashboard(has_data=False)

    with patch(
        "app.application.services.marketing_service.MarketingService.get_dashboard",
        new_callable=AsyncMock,
        return_value=tenant_b_dashboard,
    ) as mock_get:
        resp = await agent.process(
            _req(tenant_id=_TENANT_B), task=_task("analizar_marketing")
        )
        # Verificamos que se llamó con el UUID del tenant B
        call_kwargs = mock_get.call_args
        tenant_arg = call_kwargs[0][0] if call_kwargs[0] else call_kwargs[1].get("tenant_id")
        import uuid
        assert tenant_arg == uuid.UUID(_TENANT_B)

    assert resp.result["summary"] == "marketing_sin_datos"
    assert resp.confidence == Confidence.MEDIUM
