"""Expense entry endpoints."""

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_tenant, get_current_user
from app.application.services.score_trigger_service import trigger_score_recalculation
from app.persistence.db.session import get_db_session
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry
from app.persistence.models.user import User
from app.persistence.repositories.transaction_repository import ExpenseRepository
from app.schemas.common import MessageResponse
from app.schemas.transaction import (
    CreateExpenseRequest,
    ExpenseEntryResponse,
    UpdateExpenseRequest,
)

router = APIRouter()


@router.get("", response_model=list[ExpenseEntryResponse], summary="List expense entries")
async def list_expenses(
    from_date: date | None = Query(default=None),
    to_date: date | None = Query(default=None),
    category: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> list[ExpenseEntry]:
    repo = ExpenseRepository(session)
    return await repo.list_by_tenant(
        tenant.id,
        from_date=from_date,
        to_date=to_date,
        category=category,
        limit=limit,
        offset=offset,
    )


@router.post(
    "",
    response_model=ExpenseEntryResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new expense",
)
async def create_expense(
    body: CreateExpenseRequest,
    tenant: Tenant = Depends(get_current_tenant),
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> ExpenseEntry:
    repo = ExpenseRepository(session)
    entry = ExpenseEntry(
        tenant_id=tenant.id,
        amount=body.amount,
        category=body.category,
        transaction_date=body.transaction_date,
        description=body.description,
        is_recurring=body.is_recurring,
        payment_method=body.payment_method,
        supplier_name=body.supplier_name,
        notes=body.notes,
    )
    saved = await repo.save(entry)
    trigger_score_recalculation.delay(str(tenant.id), "expense_entry_created")
    return saved


@router.get(
    "/{expense_id}", response_model=ExpenseEntryResponse, summary="Get expense by ID"
)
async def get_expense(
    expense_id: UUID,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> ExpenseEntry:
    repo = ExpenseRepository(session)
    entry = await repo.get_by_id(expense_id, tenant.id)
    if not entry:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Expense not found.")
    return entry


@router.patch(
    "/{expense_id}", response_model=ExpenseEntryResponse, summary="Update an expense"
)
async def update_expense(
    expense_id: UUID,
    body: UpdateExpenseRequest,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> ExpenseEntry:
    repo = ExpenseRepository(session)
    entry = await repo.get_by_id(expense_id, tenant.id)
    if not entry:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Expense not found.")
    if body.amount is not None:
        entry.amount = body.amount
    if body.category is not None:
        entry.category = body.category
    if body.transaction_date is not None:
        entry.transaction_date = body.transaction_date
    if body.description is not None:
        entry.description = body.description
    if body.is_recurring is not None:
        entry.is_recurring = body.is_recurring
    if body.supplier_name is not None:
        entry.supplier_name = body.supplier_name
    if body.notes is not None:
        entry.notes = body.notes
    saved = await repo.save(entry)
    trigger_score_recalculation.delay(str(tenant.id), "expense_entry_updated")
    return saved


@router.delete(
    "/{expense_id}", response_model=MessageResponse, summary="Delete an expense entry"
)
async def delete_expense(
    expense_id: UUID,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> MessageResponse:
    repo = ExpenseRepository(session)
    entry = await repo.get_by_id(expense_id, tenant.id)
    if not entry:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Expense not found.")
    await repo.delete(entry)
    trigger_score_recalculation.delay(str(tenant.id), "expense_entry_deleted")
    return MessageResponse(message="Expense entry deleted.")
