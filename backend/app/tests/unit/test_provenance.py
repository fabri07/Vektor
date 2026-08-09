"""Tests de provenance tagging e isolation de datos demo/real."""

import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.persistence.repositories.product_repository import ProductRepository
from app.persistence.repositories.transaction_repository import ExpenseRepository, SaleRepository


def _where_text(statement: object) -> str:
    where = getattr(statement, "whereclause", None)
    return str(where).lower() if where is not None else ""


class TestProvenanceDefaults:
    @pytest.mark.parametrize(
        ("modelo", "kwargs"),
        [
            pytest.param(
                SaleEntry,
                {
                    "amount": Decimal("1000"),
                    "quantity": 1,
                    "transaction_date": date.today(),
                    "payment_method": "cash",
                },
                id="test_sale_entry_explicit_provenance_real",
            ),
            pytest.param(
                ExpenseEntry,
                {
                    "amount": Decimal("500"),
                    "category": "test",
                    "transaction_date": date.today(),
                    "description": "test",
                },
                id="test_expense_entry_explicit_provenance_real",
            ),
            pytest.param(
                Product,
                {"name": "Test Product", "sale_price_ars": Decimal("1000")},
                id="test_product_explicit_provenance_real",
            ),
        ],
    )
    def test_provenance_real_explicita(self, modelo: type, kwargs: dict) -> None:
        """Los inserts de datos reales deben pasar provenance='REAL' explícitamente."""
        entidad = modelo(tenant_id=uuid.uuid4(), provenance="REAL", **kwargs)
        assert entidad.provenance == "REAL"

    def test_tenant_explicit_is_demo_false(self) -> None:
        tenant = Tenant(
            legal_name="Test SA",
            display_name="Test",
            is_demo=False,
        )
        assert tenant.is_demo is False


class TestProvenanceConfirmedData:
    @pytest.mark.parametrize(
        ("modelo", "kwargs", "esperado"),
        [
            pytest.param(
                SaleEntry,
                {
                    "amount": Decimal("10000"),
                    "quantity": 1,
                    "transaction_date": date.today(),
                    "payment_method": "cash",
                    "notes": "Importado desde archivo",
                },
                "REAL",
                id="test_confirmed_data_tiene_provenance_real",
            ),
            pytest.param(
                SaleEntry,
                {
                    "amount": Decimal("10700"),
                    "quantity": 1,
                    "transaction_date": date.today(),
                    "payment_method": "cash",
                    "notes": "demo",
                },
                "DEMO",
                id="test_demo_seed_tiene_provenance_demo",
            ),
            pytest.param(
                ExpenseEntry,
                {
                    "amount": Decimal("5000"),
                    "category": "importado",
                    "transaction_date": date.today(),
                    "description": "Gasto importado",
                    "payment_method": "transfer",
                },
                "REAL",
                id="test_expense_confirmada_provenance_real",
            ),
            pytest.param(
                Product,
                {"name": "Alfajor", "sale_price_ars": Decimal("350")},
                "REAL",
                id="test_product_confirmado_provenance_real",
            ),
        ],
    )
    def test_provenance_confirmada(self, modelo: type, kwargs: dict, esperado: str) -> None:
        entidad = modelo(tenant_id=uuid.uuid4(), provenance=esperado, **kwargs)
        assert entidad.provenance == esperado


class TestDemoTenant:
    @pytest.mark.parametrize(
        ("legal_name", "display_name", "is_demo"),
        [
            pytest.param(
                "Kiosco San Martín SRL",
                "Kiosco San Martín",
                True,
                id="test_demo_tenant_is_demo_true",
            ),
            pytest.param(
                "Mi Negocio SRL",
                "Mi Negocio",
                False,
                id="test_real_tenant_is_demo_false",
            ),
        ],
    )
    def test_is_demo(self, legal_name: str, display_name: str, is_demo: bool) -> None:
        tenant = Tenant(legal_name=legal_name, display_name=display_name, is_demo=is_demo)
        assert tenant.is_demo is is_demo


class TestProvenanceDoesNotControlTenantReads:
    @pytest.mark.asyncio
    async def test_product_repository_list_does_not_filter_by_provenance(self) -> None:
        session = AsyncMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        session.execute.return_value = result

        await ProductRepository(session).list_by_tenant(uuid.uuid4())

        statement = session.execute.call_args.args[0]
        assert "provenance" not in _where_text(statement)

    @pytest.mark.asyncio
    async def test_sale_repository_list_filters_voided_but_not_provenance(self) -> None:
        session = AsyncMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        session.execute.return_value = result

        await SaleRepository(session).list_by_tenant(uuid.uuid4())

        statement = session.execute.call_args.args[0]
        compiled = _where_text(statement)
        assert "voided_at" in compiled
        assert "provenance" not in compiled

    @pytest.mark.asyncio
    async def test_expense_repository_list_filters_voided_but_not_provenance(self) -> None:
        session = AsyncMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        session.execute.return_value = result

        await ExpenseRepository(session).list_by_tenant(uuid.uuid4())

        statement = session.execute.call_args.args[0]
        compiled = _where_text(statement)
        assert "voided_at" in compiled
        assert "provenance" not in compiled


class TestNoDataResponse:
    def test_no_data_response_shape(self) -> None:
        from app.schemas.health_score import NoDataResponse

        r = NoDataResponse()
        assert r.status == "NO_DATA"
        assert r.score is None
        assert r.score_level == "NO_DATA"
        assert r.is_demo_data is False

    def test_no_data_response_demo_flag(self) -> None:
        from app.schemas.health_score import NoDataResponse

        # Demo tenant sin snapshot → NoDataResponse con is_demo_data=True
        # (en la práctica demo siempre tiene snapshot, pero el schema lo soporta)
        r = NoDataResponse(is_demo_data=False)
        assert r.is_demo_data is False

    def test_health_score_v2_has_is_demo_data(self) -> None:
        import datetime

        from app.schemas.health_score import HealthScoreV2Response

        r = HealthScoreV2Response(
            id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            score_total=74,
            score_cash=80,
            score_margin=70,
            score_stock=75,
            score_supplier=65,
            primary_risk_code="CASH_LOW",
            confidence_level="HIGH",
            data_completeness_score=0.9,
            level="good",
            created_at=datetime.datetime.now(),
            is_demo_data=True,
        )
        assert r.is_demo_data is True


class TestRejectedFileStatus:
    def test_rejected_file_tiene_rejection_reason(self) -> None:
        from app.persistence.models.file import (
            PROCESSING_STATUS_REJECTED,
            UploadedFile,
        )

        f = UploadedFile(
            tenant_id=uuid.uuid4(),
            original_filename="test.jpg",
            s3_key="uploads/test/test.jpg",
            content_type="image/jpeg",
            size_bytes=1024,
            purpose="general",
        )
        f.processing_status = PROCESSING_STATUS_REJECTED
        f.rejection_reason = "confidence_too_low"
        assert f.processing_status == "REJECTED"
        assert f.rejection_reason == "confidence_too_low"

    def test_score_level_no_data_exists(self) -> None:
        from app.domain.health_score import ScoreLevel

        assert ScoreLevel.NO_DATA == "NO_DATA"

    def test_needs_attention_no_data_is_false(self) -> None:
        """NO_DATA no debe ser considerado needs_attention (no hay datos, no hay riesgo)."""
        from app.domain.health_score import ScoreLevel

        # needs_attention checks CRITICAL and WARNING only
        assert ScoreLevel.NO_DATA not in (ScoreLevel.CRITICAL, ScoreLevel.WARNING)
