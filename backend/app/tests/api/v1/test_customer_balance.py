"""Tests F2a: cobro→cliente link, saldo neto, credit_limit, endpoint de saldo.

Cubre:
- save_cash_inflow vincula customer_id cuando viene en entities (y deja None si no viene).
- SaleRepository.get_balance_by_customer: total_account / total_paid / balance correctos.
- SaleRepository.get_balances_by_customer: ranking por balance desc, excluye NULL.
- GET /customers/{id}/balance: saldo correcto + over_limit.
- Cross-tenant: GET /customers/{otro_tenant_id}/balance → 404.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.customer import Customer
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.repositories.transaction_repository import SaleRepository

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

_DNI_SEQ = iter(range(30_000_000, 39_999_999))


def _person(name: str, **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": name,
        "customer_type": "person",
        "last_name": "Test",
        "dni": str(next(_DNI_SEQ)),
        "phone": "+54 11 5555-9999",
    }
    base.update(extra)
    return base


def _sale_entry(
    *,
    tenant_id: uuid.UUID,
    customer_id: uuid.UUID | None,
    amount: str,
    payment_method: str,
) -> SaleEntry:
    return SaleEntry(
        tenant_id=tenant_id,
        customer_id=customer_id,
        amount=Decimal(amount),
        quantity=1,
        transaction_date=datetime.now(UTC),
        payment_method=payment_method,
        provenance="REAL",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Fixture: cliente + ventas (account x2 + inflow x1)
# ─────────────────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def customer_with_sales(
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> Customer:
    customer = Customer(tenant_id=sample_tenant.tenant_id, name="Juan Comprador")
    db_session.add(customer)
    await db_session.flush()

    db_session.add_all(
        [
            _sale_entry(
                tenant_id=sample_tenant.tenant_id,
                customer_id=customer.id,
                amount="1000.00",
                payment_method="account",
            ),
            _sale_entry(
                tenant_id=sample_tenant.tenant_id,
                customer_id=customer.id,
                amount="500.00",
                payment_method="account",
            ),
            _sale_entry(
                tenant_id=sample_tenant.tenant_id,
                customer_id=customer.id,
                amount="300.00",
                payment_method="inflow",
            ),
        ]
    )
    await db_session.commit()
    return customer


# ─────────────────────────────────────────────────────────────────────────────
# 1. Servicio: vínculo cobro → cliente
# ─────────────────────────────────────────────────────────────────────────────


class TestCashInflowLinksCustomer:
    async def test_with_customer_id(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """save_cash_inflow con customer_id en entities → SaleEntry queda vinculado."""
        from app.application.services.cash_service import save_cash_inflow  # noqa: PLC0415

        cid = uuid.uuid4()
        entry = await save_cash_inflow(
            {"amount": "500", "customer_id": str(cid)},
            sample_tenant.tenant_id,
            db_session,
        )
        assert entry.customer_id == cid
        assert entry.payment_method == "inflow"

    async def test_without_customer_id(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """save_cash_inflow sin customer_id → customer_id queda None."""
        from app.application.services.cash_service import save_cash_inflow  # noqa: PLC0415

        entry = await save_cash_inflow(
            {"amount": "300"},
            sample_tenant.tenant_id,
            db_session,
        )
        assert entry.customer_id is None
        assert entry.payment_method == "inflow"


# ─────────────────────────────────────────────────────────────────────────────
# 2. Repository: get_balance_by_customer
# ─────────────────────────────────────────────────────────────────────────────


class TestGetBalanceByCustomer:
    async def test_balance_correct(
        self,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        customer_with_sales: Customer,
    ) -> None:
        """2 ventas account ($1000+$500) + 1 inflow ($300) → balance $1200."""
        repo = SaleRepository(db_session)
        result = await repo.get_balance_by_customer(
            sample_tenant.tenant_id, customer_with_sales.id
        )
        assert result["total_account"] == 1500.0
        assert result["total_paid"] == 300.0
        assert result["balance"] == 1200.0

    async def test_balance_no_sales_returns_zeros(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Cliente sin ventas → todos 0.0 (no None)."""
        repo = SaleRepository(db_session)
        result = await repo.get_balance_by_customer(sample_tenant.tenant_id, uuid.uuid4())
        assert result["total_account"] == 0.0
        assert result["total_paid"] == 0.0
        assert result["balance"] == 0.0

    async def test_voided_excluded(
        self,
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        """Ventas anuladas no cuentan en el saldo."""
        customer = Customer(tenant_id=sample_tenant.tenant_id, name="Anulado Test")
        db_session.add(customer)
        await db_session.flush()

        # Venta válida + venta anulada
        db_session.add_all(
            [
                _sale_entry(
                    tenant_id=sample_tenant.tenant_id,
                    customer_id=customer.id,
                    amount="400.00",
                    payment_method="account",
                ),
                SaleEntry(
                    tenant_id=sample_tenant.tenant_id,
                    customer_id=customer.id,
                    amount=Decimal("999.00"),
                    quantity=1,
                    transaction_date=datetime.now(UTC),
                    payment_method="account",
                    provenance="REAL",
                    voided_at=datetime.now(UTC),
                    void_reason="USER_CANCELLED",
                ),
            ]
        )
        await db_session.commit()

        repo = SaleRepository(db_session)
        result = await repo.get_balance_by_customer(sample_tenant.tenant_id, customer.id)
        assert result["total_account"] == 400.0


# ─────────────────────────────────────────────────────────────────────────────
# 3. Repository: get_balances_by_customer
# ─────────────────────────────────────────────────────────────────────────────


class TestGetBalancesByCustomer:
    async def test_ranking_ordered_by_balance_desc(
        self,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        customer_with_sales: Customer,
    ) -> None:
        """Resultado ordenado por balance desc."""
        c2 = Customer(tenant_id=sample_tenant.tenant_id, name="Ana Deudora")
        db_session.add(c2)
        await db_session.flush()
        db_session.add(
            _sale_entry(
                tenant_id=sample_tenant.tenant_id,
                customer_id=c2.id,
                amount="100.00",
                payment_method="account",
            )
        )
        await db_session.commit()

        repo = SaleRepository(db_session)
        rows = await repo.get_balances_by_customer(sample_tenant.tenant_id)

        assert len(rows) >= 2
        balances = [r["balance"] for r in rows]
        assert balances == sorted(balances, reverse=True)

    async def test_excludes_null_customer_id(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Ventas sin customer_id no aparecen en el ranking."""
        db_session.add(
            _sale_entry(
                tenant_id=sample_tenant.tenant_id,
                customer_id=None,
                amount="200.00",
                payment_method="account",
            )
        )
        await db_session.commit()

        repo = SaleRepository(db_session)
        rows = await repo.get_balances_by_customer(sample_tenant.tenant_id)
        for row in rows:
            assert row["customer_id"] is not None
            assert row["customer_id"] != "None"

    async def test_keys_present(
        self,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        customer_with_sales: Customer,
    ) -> None:
        """Cada fila tiene las claves esperadas."""
        repo = SaleRepository(db_session)
        rows = await repo.get_balances_by_customer(sample_tenant.tenant_id)
        assert rows, "Debería haber al menos una fila"
        row = rows[0]
        for key in (
            "customer_id", "customer_name", "total_account", "total_paid", "balance", "n_sales"
        ):
            assert key in row, f"Falta clave: {key}"


# ─────────────────────────────────────────────────────────────────────────────
# 4. API: GET /customers/{id}/balance
# ─────────────────────────────────────────────────────────────────────────────


class TestCustomerBalanceEndpoint:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger: Any) -> None:
        pass

    async def test_balance_endpoint_correct_values(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        # Crear cliente vía API
        resp = await client.post(
            "/api/v1/customers", json=_person("Nicolás Fiado"), headers=auth_headers
        )
        assert resp.status_code == 201
        cid = resp.json()["id"]
        customer_id = uuid.UUID(cid)

        # Insertar ventas directamente en DB
        db_session.add_all(
            [
                _sale_entry(
                    tenant_id=sample_tenant.tenant_id,
                    customer_id=customer_id,
                    amount="800.00",
                    payment_method="account",
                ),
                _sale_entry(
                    tenant_id=sample_tenant.tenant_id,
                    customer_id=customer_id,
                    amount="200.00",
                    payment_method="inflow",
                ),
            ]
        )
        await db_session.commit()

        resp = await client.get(f"/api/v1/customers/{cid}/balance", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["customer_id"] == cid
        assert body["total_account"] == 800.0
        assert body["total_paid"] == 200.0
        assert body["balance"] == 600.0
        assert body["credit_limit"] is None
        assert body["over_limit"] is False

    async def test_over_limit_true(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        """balance > credit_limit → over_limit=True."""
        resp = await client.post(
            "/api/v1/customers", json=_person("María Límite"), headers=auth_headers
        )
        assert resp.status_code == 201
        cid = resp.json()["id"]
        customer_id = uuid.UUID(cid)

        # Setear credit_limit directamente en DB
        from app.persistence.repositories.customer_repository import (
            CustomerRepository,  # noqa: PLC0415
        )
        repo = CustomerRepository(db_session)
        customer = await repo.get_by_id(customer_id, sample_tenant.tenant_id)
        assert customer is not None
        customer.credit_limit = Decimal("500.00")
        await db_session.commit()

        db_session.add(
            _sale_entry(
                tenant_id=sample_tenant.tenant_id,
                customer_id=customer_id,
                amount="600.00",
                payment_method="account",
            )
        )
        await db_session.commit()

        resp = await client.get(f"/api/v1/customers/{cid}/balance", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["balance"] == 600.0
        assert float(body["credit_limit"]) == 500.0
        assert body["over_limit"] is True

    async def test_over_limit_false_when_under(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        """balance < credit_limit → over_limit=False."""
        resp = await client.post(
            "/api/v1/customers", json=_person("Pedro Bajo"), headers=auth_headers
        )
        assert resp.status_code == 201
        cid = resp.json()["id"]
        customer_id = uuid.UUID(cid)

        from app.persistence.repositories.customer_repository import (
            CustomerRepository,  # noqa: PLC0415
        )
        repo = CustomerRepository(db_session)
        customer = await repo.get_by_id(customer_id, sample_tenant.tenant_id)
        assert customer is not None
        customer.credit_limit = Decimal("1000.00")
        await db_session.commit()

        db_session.add(
            _sale_entry(
                tenant_id=sample_tenant.tenant_id,
                customer_id=customer_id,
                amount="300.00",
                payment_method="account",
            )
        )
        await db_session.commit()

        resp = await client.get(f"/api/v1/customers/{cid}/balance", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["over_limit"] is False

    async def test_balance_endpoint_404_nonexistent(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
    ) -> None:
        resp = await client.get(
            f"/api/v1/customers/{uuid.uuid4()}/balance", headers=auth_headers
        )
        assert resp.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# 5. Cross-tenant isolation (obligatorio)
# ─────────────────────────────────────────────────────────────────────────────


class TestBalanceCrossTenant:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger: Any) -> None:
        pass

    async def test_cross_tenant_returns_404(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        second_auth_headers: dict[str, Any],
    ) -> None:
        """Tenant 2 no puede leer el saldo de un cliente de Tenant 1 → 404."""
        # Crear cliente en tenant 1
        resp = await client.post(
            "/api/v1/customers",
            json=_person("Cliente Tenant Uno"),
            headers=auth_headers,
        )
        assert resp.status_code == 201
        cid = resp.json()["id"]

        # Tenant 2 intenta acceder → 404
        resp2 = await client.get(
            f"/api/v1/customers/{cid}/balance",
            headers=second_auth_headers,
        )
        assert resp2.status_code == 404
