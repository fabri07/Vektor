"""Tests para los endpoints de Grupo C: cash-breakdown, margin-history y templates LLM."""

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from httpx import AsyncClient

from app.heuristics.insight_templates import render_insight

_TODAY = str(date.today())

_SALE_PAYLOAD = {
    "amount": "5000.00",
    "quantity": 1,
    "transaction_date": _TODAY,
    "payment_method": "cash",
}
_EXPENSE_PAYLOAD = {
    "amount": "2000.00",
    "category": "RENT",
    "expense_date": _TODAY,
}


# ── GET /insights/cash-breakdown ──────────────────────────────────────────────


@pytest.mark.asyncio
class TestCashBreakdown:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_empty_response_structure(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        """Sin datos, el endpoint retorna estructura válida con series vacías."""
        resp = await client.get(
            "/api/v1/insights/cash-breakdown?days=30", headers=auth_headers
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "dates" in data
        assert "income_series" in data
        assert "expense_series" in data
        assert data["granularity"] == "daily"
        assert data["days"] == 30

    async def test_income_series_populated_after_sale(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        """Al registrar una venta, income_series refleja el método de pago."""
        await client.post("/api/v1/sales", json=_SALE_PAYLOAD, headers=auth_headers)

        resp = await client.get(
            "/api/v1/insights/cash-breakdown?days=30", headers=auth_headers
        )
        data = resp.json()
        assert _TODAY in data["dates"]
        assert "cash" in data["income_series"]
        idx = data["dates"].index(_TODAY)
        assert data["income_series"]["cash"][idx] == pytest.approx(5000.0)

    async def test_expense_series_populated_after_expense(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        """Al registrar un gasto, expense_series refleja el método de pago."""
        await client.post("/api/v1/expenses", json=_EXPENSE_PAYLOAD, headers=auth_headers)

        resp = await client.get(
            "/api/v1/insights/cash-breakdown?days=30", headers=auth_headers
        )
        data = resp.json()
        assert _TODAY in data["dates"]
        assert len(data["expense_series"]) >= 1

    async def test_weekly_granularity(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        """?granularity=weekly agrupa por semana — todas las fechas deben ser lunes."""
        await client.post("/api/v1/sales", json=_SALE_PAYLOAD, headers=auth_headers)

        resp = await client.get(
            "/api/v1/insights/cash-breakdown?days=30&granularity=weekly",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["granularity"] == "weekly"
        for d in data["dates"]:
            parsed = date.fromisoformat(d)
            assert parsed.weekday() == 0, f"{d} no es lunes"

    async def test_invalid_granularity_rejected(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        resp = await client.get(
            "/api/v1/insights/cash-breakdown?granularity=monthly", headers=auth_headers
        )
        assert resp.status_code == 422


# ── GET /health-scores/margin-history ─────────────────────────────────────────


@pytest.mark.asyncio
class TestMarginHistory:
    async def test_empty_when_no_snapshots(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        resp = await client.get(
            "/api/v1/health-scores/margin-history", headers=auth_headers
        )
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_response_fields_present(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        """Schema tiene todos los campos requeridos (puede estar vacío)."""
        resp = await client.get(
            "/api/v1/health-scores/margin-history?limit=6", headers=auth_headers
        )
        assert resp.status_code == 200
        for item in resp.json():
            assert "snapshot_date" in item
            assert "score_margin" in item
            assert "revenue_est" in item
            assert "cogs_est" in item
            assert "fixed_expenses_est" in item
            assert "margin_pct" in item


# ── insight_templates CASH_LOW severity variants ──────────────────────────────


def _make_cash_state(caja: str, gastos: str) -> MagicMock:
    state = MagicMock()
    state.monthly_fixed_expenses_est = Decimal(gastos)
    state.cash_on_hand_est = Decimal(caja)
    state.products = []
    return state


def _make_snapshot(score_cash: int) -> MagicMock:
    snap = MagicMock()
    snap.score_cash = score_cash
    return snap


def test_cash_low_critical_description():
    """score_cash < 30 → descripción crítica."""
    state = _make_cash_state("5000", "100000")
    snapshot = _make_snapshot(score_cash=20)
    _, description, _ = render_insight("CASH_LOW", state, snapshot)
    assert "crítica" in description.lower()


def test_cash_low_warning_description():
    """30 <= score_cash < 60 → descripción de advertencia."""
    state = _make_cash_state("15000", "100000")
    snapshot = _make_snapshot(score_cash=45)
    _, description, _ = render_insight("CASH_LOW", state, snapshot)
    assert "ajustado" in description.lower()
    assert "crítica" not in description.lower()


def test_cash_low_template_fallback():
    """score_cash >= 60 → template por defecto."""
    state = _make_cash_state("30000", "100000")
    snapshot = _make_snapshot(score_cash=65)
    _, description, _ = render_insight("CASH_LOW", state, snapshot)
    assert "cobertura operativa es limitada" in description


# ── POST /health-scores/{id}/export ───────────────────────────────────────────


async def _create_snapshot(db_session, tenant_id, score_growth=70) -> "HealthScoreSnapshot":
    """Helper: inserta un snapshot mínimo para testear el export."""
    from datetime import UTC, datetime as _dt  # noqa: PLC0415
    from app.persistence.models.score import HealthScoreSnapshot  # noqa: PLC0415

    now = _dt.now(UTC)
    snap = HealthScoreSnapshot(
        tenant_id=tenant_id,
        total_score=Decimal("75"),
        level="good",
        dimensions=[],
        triggered_by="test",
        snapshot_date=now,
        created_at=now,
        score_cash=70,
        score_margin=80,
        score_stock=75,
        score_supplier=85,
        score_growth=score_growth,
        primary_risk_code="CASH_LOW",
        confidence_level="HIGH",
        data_completeness_score=Decimal("90.0"),
    )
    db_session.add(snap)
    await db_session.commit()
    await db_session.refresh(snap)
    return snap


@pytest.mark.asyncio
class TestExportHealthReport:
    async def test_export_pdf_success(
        self, client: AsyncClient, auth_headers: dict, sample_tenant, db_session
    ) -> None:
        snap = await _create_snapshot(db_session, sample_tenant.tenant_id)
        resp = await client.post(
            f"/api/v1/health-scores/{snap.id}/export",
            json={"format": "pdf", "narrative": "Análisis de prueba."},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content.startswith(b"%PDF")
        assert "attachment" in resp.headers["content-disposition"].lower()

    async def test_export_docx_success(
        self, client: AsyncClient, auth_headers: dict, sample_tenant, db_session
    ) -> None:
        snap = await _create_snapshot(db_session, sample_tenant.tenant_id)
        resp = await client.post(
            f"/api/v1/health-scores/{snap.id}/export",
            json={"format": "docx", "narrative": ""},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert "wordprocessingml" in resp.headers["content-type"]
        assert resp.content[:4] == b"PK\x03\x04"

    async def test_export_invalid_format_rejected(
        self, client: AsyncClient, auth_headers: dict, sample_tenant, db_session
    ) -> None:
        snap = await _create_snapshot(db_session, sample_tenant.tenant_id)
        resp = await client.post(
            f"/api/v1/health-scores/{snap.id}/export",
            json={"format": "html", "narrative": ""},
            headers=auth_headers,
        )
        assert resp.status_code == 422

    async def test_export_narrative_too_long_rejected(
        self, client: AsyncClient, auth_headers: dict, sample_tenant, db_session
    ) -> None:
        snap = await _create_snapshot(db_session, sample_tenant.tenant_id)
        resp = await client.post(
            f"/api/v1/health-scores/{snap.id}/export",
            json={"format": "pdf", "narrative": "x" * 5000},
            headers=auth_headers,
        )
        assert resp.status_code == 422

    async def test_export_other_tenant_returns_404(
        self,
        client: AsyncClient,
        auth_headers: dict,
        sample_tenant,
        db_session,
    ) -> None:
        """Snapshot de otro tenant → 404 (no se filtra el snapshot ajeno)."""
        import uuid as _uuid  # noqa: PLC0415

        other_tenant_id = _uuid.uuid4()
        # Crear tenant para satisfacer FK
        from app.persistence.models.tenant import Tenant  # noqa: PLC0415

        other = Tenant(tenant_id=other_tenant_id, legal_name="Otro SA", display_name="Otro")
        db_session.add(other)
        await db_session.commit()
        snap = await _create_snapshot(db_session, other_tenant_id)
        resp = await client.post(
            f"/api/v1/health-scores/{snap.id}/export",
            json={"format": "pdf", "narrative": ""},
            headers=auth_headers,
        )
        assert resp.status_code == 404

    async def test_export_nonexistent_snapshot_returns_404(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        import uuid as _uuid  # noqa: PLC0415

        resp = await client.post(
            f"/api/v1/health-scores/{_uuid.uuid4()}/export",
            json={"format": "pdf", "narrative": ""},
            headers=auth_headers,
        )
        assert resp.status_code == 404
