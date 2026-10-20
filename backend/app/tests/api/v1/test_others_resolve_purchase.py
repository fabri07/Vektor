"""Resolución transaccional de compras ambiguas desde Otros."""

import uuid
from datetime import datetime, time
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agents.shared.event_bus import EventBus
from app.domain.business_time import today_ar
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.supplier import Supplier
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry
from app.persistence.models.unclassified_record import (
    UNCLASSIFIED_STATUS_IMPORTED,
    UNCLASSIFIED_STATUS_PENDING,
    UnclassifiedRecord,
)


async def _product(
    session: AsyncSession, tenant_id: uuid.UUID, **overrides: Any
) -> Product:
    values: dict[str, Any] = {
        "tenant_id": tenant_id,
        "name": "Agua mineral",
        "sale_price_ars": Decimal("150.00"),
        "unit_cost_ars": Decimal("50.00"),
        "stock_units": 3,
        "provenance": "REAL",
    }
    values.update(overrides)
    product = Product(**values)
    session.add(product)
    await session.commit()
    return product


async def _record(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    product_id: uuid.UUID,
    *,
    suggested_entity: str = "expense",
    candidates: bool = True,
) -> UnclassifiedRecord:
    record = UnclassifiedRecord(
        tenant_id=tenant_id,
        source="ingestion",
        context_label="Compra ambigua",
        headers=["producto", "total", "cantidad"],
        row_data={"producto": "Agua", "total": "600", "cantidad": "4"},
        suggested_entity=suggested_entity,
        match_candidates=(
            [{"id": str(product_id), "matched_by": ["name"], "name": "Agua"}]
            if candidates
            else None
        ),
        status=UNCLASSIFIED_STATUS_PENDING,
    )
    session.add(record)
    await session.commit()
    return record


def _payload(product_id: uuid.UUID) -> dict[str, Any]:
    return {
        "target_product_id": str(product_id),
        "amount": "600.00",
        "quantity": 4,
        "transaction_date": f"{today_ar().isoformat()}T12:00:00",
        "payment_method": "transfer",
        "category": "INVENTORY",
        "description": "Compra de agua",
    }


@pytest.fixture(autouse=True)
def defer_events(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(EventBus, "emit_after_commit", lambda *_args, **_kwargs: None)


async def test_resolve_purchase_creates_expense_movement_and_sentinel_once(
    client: AsyncClient,
    db_session: AsyncSession,
    auth_headers: dict[str, Any],
    sample_tenant: Tenant,
) -> None:
    target = await _product(db_session, sample_tenant.tenant_id)
    record = await _record(db_session, sample_tenant.tenant_id, target.id)

    response = await client.post(
        f"/api/v1/others/{record.id}/resolve-purchase",
        json=_payload(target.id),
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text

    expense = (
        await db_session.execute(
            select(ExpenseEntry).where(ExpenseEntry.product_id == target.id)
        )
    ).scalar_one()
    movement = (
        await db_session.execute(
            select(InventoryMovement).where(InventoryMovement.product_id == target.id)
        )
    ).scalar_one()
    supplier = await db_session.get(Supplier, expense.supplier_id)
    await db_session.refresh(target)
    await db_session.refresh(record)

    assert expense.expense_type == "COGS"
    assert movement.movement_type == "purchase"
    assert movement.qty == 4
    assert movement.unit_cost == Decimal("150.00")
    # M·1b: el contrato del costo declarado; envío e impuesto, desconocidos.
    assert movement.cost_contract_version == 1
    assert movement.unit_cost_final == Decimal("150.00")
    assert (movement.shipping_treatment, movement.tax_treatment) == (
        "desconocido",
        "desconocido",
    )
    # Fecha de negocio en hora AR rotulada: el mediodía tipeado queda mediodía.
    assert movement.occurred_at is not None
    assert movement.occurred_at.replace(tzinfo=None) == datetime.combine(
        today_ar(), time(12)
    )
    assert target.stock_units == 7
    assert target.unit_cost_ars == Decimal("150.00")
    assert supplier is not None and supplier.name == "No identificado"
    assert supplier.custom_fields["_sentinel"] == "true"
    assert record.status == UNCLASSIFIED_STATUS_IMPORTED

    retry = await client.post(
        f"/api/v1/others/{record.id}/resolve-purchase",
        json=_payload(target.id),
        headers=auth_headers,
    )
    assert retry.status_code == 409
    assert (
        await db_session.scalar(
            select(func.count(ExpenseEntry.id)).where(ExpenseEntry.product_id == target.id)
        )
    ) == 1
    assert (
        await db_session.scalar(
            select(func.count(InventoryMovement.id)).where(
                InventoryMovement.product_id == target.id
            )
        )
    ) == 1


async def test_resolve_purchase_accepts_any_active_product_not_only_candidates(
    client: AsyncClient,
    db_session: AsyncSession,
    auth_headers: dict[str, Any],
    sample_tenant: Tenant,
) -> None:
    """B·5b: el usuario dice qué artículo es; no está limitado a lo que sugirió
    el import."""
    candidate = await _product(db_session, sample_tenant.tenant_id)
    other = await _product(db_session, sample_tenant.tenant_id, name="Otra")
    record = await _record(db_session, sample_tenant.tenant_id, candidate.id)

    response = await client.post(
        f"/api/v1/others/{record.id}/resolve-purchase",
        json=_payload(other.id),
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    await db_session.refresh(other)
    assert other.stock_units == 7
    await db_session.refresh(record)
    assert record.status == UNCLASSIFIED_STATUS_IMPORTED


async def test_resolve_purchase_rejects_candidate_from_other_tenant(
    client: AsyncClient,
    db_session: AsyncSession,
    auth_headers: dict[str, Any],
    second_auth_headers: dict[str, Any],
    sample_tenant: Tenant,
    mock_score_trigger: Any,
) -> None:
    create_response = await client.post(
        "/api/v1/products",
        json={"name": "Producto ajeno", "sale_price_ars": "10.00", "stock_units": 0},
        headers=second_auth_headers,
    )
    assert create_response.status_code == 201
    other_id = uuid.UUID(create_response.json()["id"])
    record = await _record(db_session, sample_tenant.tenant_id, other_id)

    response = await client.post(
        f"/api/v1/others/{record.id}/resolve-purchase",
        json=_payload(other_id),
        headers=auth_headers,
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INVALID_PURCHASE"
    await db_session.refresh(record)
    assert record.status == UNCLASSIFIED_STATUS_PENDING


async def test_resolve_purchase_rejects_zero_quantity(
    client: AsyncClient,
    db_session: AsyncSession,
    auth_headers: dict[str, Any],
    sample_tenant: Tenant,
) -> None:
    target = await _product(db_session, sample_tenant.tenant_id)
    record = await _record(db_session, sample_tenant.tenant_id, target.id)

    response = await client.post(
        f"/api/v1/others/{record.id}/resolve-purchase",
        json={**_payload(target.id), "quantity": 0},
        headers=auth_headers,
    )
    # quantity > 0 lo valida el schema (Pydantic) → 422, sin efectos.
    assert response.status_code == 422
    assert (
        await db_session.scalar(
            select(func.count(ExpenseEntry.id)).where(ExpenseEntry.product_id == target.id)
        )
    ) == 0
    assert (
        await db_session.scalar(
            select(func.count(InventoryMovement.id)).where(
                InventoryMovement.product_id == target.id
            )
        )
    ) == 0
    await db_session.refresh(record)
    assert record.status == UNCLASSIFIED_STATUS_PENDING


async def test_resolve_purchase_legacy_unit_cost_divergent_from_total_is_rejected(
    client: AsyncClient,
    db_session: AsyncSession,
    auth_headers: dict[str, Any],
    sample_tenant: Tenant,
) -> None:
    """B·5b: antes un costo unitario distinto del total/cantidad se respetaba
    («el total puede incluir impuestos o flete»). Ahora el IVA y el envío tienen
    su campo y entran por la regla única; con el body viejo, una divergencia es
    ambigua y se rechaza sin efectos."""
    target = await _product(db_session, sample_tenant.tenant_id)
    record = await _record(db_session, sample_tenant.tenant_id, target.id)

    response = await client.post(
        f"/api/v1/others/{record.id}/resolve-purchase",
        json={**_payload(target.id), "unit_cost": "100.00"},
        headers=auth_headers,
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "AMOUNT_MISMATCH"
    await db_session.refresh(target)
    assert target.stock_units == 3


async def test_resolve_purchase_rejects_inactive_candidate(
    client: AsyncClient,
    db_session: AsyncSession,
    auth_headers: dict[str, Any],
    sample_tenant: Tenant,
) -> None:
    # Un candidato que quedó inactivo entre la captura y la resolución: es candidato
    # (su id está en match_candidates) pero get_by_id + is_active lo rechaza.
    target = await _product(db_session, sample_tenant.tenant_id, is_active=False)
    record = await _record(db_session, sample_tenant.tenant_id, target.id)

    response = await client.post(
        f"/api/v1/others/{record.id}/resolve-purchase",
        json=_payload(target.id),
        headers=auth_headers,
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INVALID_PURCHASE"
    await db_session.refresh(record)
    assert record.status == UNCLASSIFIED_STATUS_PENDING


@pytest.mark.parametrize(
    ("suggested_entity", "candidates", "esperado"),
    [("product", True, 422), ("expense", False, 200)],
)
async def test_resolve_purchase_record_kinds(
    client: AsyncClient,
    db_session: AsyncSession,
    auth_headers: dict[str, Any],
    sample_tenant: Tenant,
    suggested_entity: str,
    candidates: bool,
    esperado: int,
) -> None:
    """B·5b: cualquier fila de gasto se puede resolver como compra (no sólo las
    ambiguas); una sugerida como producto no es una compra."""
    target = await _product(db_session, sample_tenant.tenant_id)
    record = await _record(
        db_session,
        sample_tenant.tenant_id,
        target.id,
        suggested_entity=suggested_entity,
        candidates=candidates,
    )
    response = await client.post(
        f"/api/v1/others/{record.id}/resolve-purchase",
        json=_payload(target.id),
        headers=auth_headers,
    )
    assert response.status_code == esperado, response.text
    if esperado == 422:
        assert response.json()["detail"]["code"] == "NOT_A_PURCHASE"
