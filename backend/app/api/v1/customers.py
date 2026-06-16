"""Customer (clientes) CRUD endpoints."""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_tenant, require_role
from app.application.services.idempotency import claim_idempotency_key
from app.persistence.db.session import get_db_session
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.customer import Customer
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.persistence.repositories.customer_repository import CustomerRepository
from app.schemas.common import MessageResponse
from app.schemas.customer import (
    CreateCustomerRequest,
    CustomerResponse,
    UpdateCustomerRequest,
)

router = APIRouter()


def _customer_snapshot(customer: Customer) -> dict[str, object]:
    return {
        "id": str(customer.id),
        "name": customer.name,
        "email": customer.email,
        "phone": customer.phone,
        "telegram_username": customer.telegram_username,
        "notes": customer.notes,
        "custom_fields": customer.custom_fields,
        "deactivated_at": (
            customer.deactivated_at.isoformat() if customer.deactivated_at else None
        ),
    }


def _audit_data_change(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    user_id: UUID,
    decision_type: str,
    before: dict[str, object],
    after: dict[str, object],
) -> None:
    session.add(
        DecisionAuditLog(
            tenant_id=tenant_id,
            decision_type=decision_type,
            decision_data={
                "record_type": "customer",
                "record_id": before["id"],
                "before": before,
                "after": after,
                "source": "ui",
            },
            triggered_by="ui:data_records",
            actor_user_id=user_id,
            context={"endpoint": "customers"},
            created_at=datetime.now(UTC),
        )
    )


@router.get("", response_model=list[CustomerResponse], summary="List customers")
async def list_customers(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> list[Customer]:
    repo = CustomerRepository(session)
    return await repo.list_by_tenant(tenant.tenant_id, limit=limit, offset=offset)


@router.post(
    "",
    response_model=CustomerResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a customer",
)
async def create_customer(
    body: CreateCustomerRequest,
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(require_role("OWNER", "ADMIN")),
    session: AsyncSession = Depends(get_db_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Customer:
    # Idempotencia opcional: si llega la key, reclamarla ANTES de crear. Comparte
    # transacción con la creación (commit único en get_db_session): si la creación
    # falla el claim se revierte y un reintento futuro puede entrar.
    if idempotency_key is not None and not await claim_idempotency_key(
        session, tenant.tenant_id, idempotency_key, "IDEMPOTENT_POST_CUSTOMER"
    ):
        raise HTTPException(status_code=409, detail={"code": "DUPLICATE_IDEMPOTENT"})
    repo = CustomerRepository(session)
    customer = Customer(tenant_id=tenant.tenant_id, **body.model_dump())
    saved = await repo.save(customer)
    _audit_data_change(
        session,
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        decision_type="DATA_RECORD_CREATED",
        before=_customer_snapshot(saved),
        after=_customer_snapshot(saved),
    )
    return saved


@router.get("/{customer_id}", response_model=CustomerResponse, summary="Get customer by ID")
async def get_customer(
    customer_id: UUID,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> Customer:
    repo = CustomerRepository(session)
    customer = await repo.get_by_id(customer_id, tenant.tenant_id)
    if not customer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found.")
    return customer


@router.patch("/{customer_id}", response_model=CustomerResponse, summary="Update a customer")
async def update_customer(
    customer_id: UUID,
    body: UpdateCustomerRequest,
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(require_role("OWNER", "ADMIN")),
    session: AsyncSession = Depends(get_db_session),
) -> Customer:
    repo = CustomerRepository(session)
    customer = await repo.get_by_id(customer_id, tenant.tenant_id)
    if not customer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found.")
    before = _customer_snapshot(customer)
    # exclude_unset: aplica solo los campos enviados; deja intactos los no enviados.
    updates = body.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(customer, field, value)
    saved = await repo.save(customer)
    _audit_data_change(
        session,
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        decision_type="DATA_RECORD_UPDATED",
        before=before,
        after=_customer_snapshot(saved),
    )
    return saved


@router.delete(
    "/{customer_id}", response_model=MessageResponse, summary="Soft-delete a customer"
)
async def delete_customer(
    customer_id: UUID,
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(require_role("OWNER", "ADMIN")),
    session: AsyncSession = Depends(get_db_session),
) -> MessageResponse:
    repo = CustomerRepository(session)
    customer = await repo.get_by_id(customer_id, tenant.tenant_id)
    if not customer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found.")
    before = _customer_snapshot(customer)
    await repo.soft_delete(customer)
    _audit_data_change(
        session,
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        decision_type="DATA_RECORD_VOIDED",
        before=before,
        after=_customer_snapshot(customer),
    )
    return MessageResponse(message="Customer deactivated.")
