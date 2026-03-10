"""Sales entry endpoints."""

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_tenant, get_current_user
from app.application.services.score_trigger_service import trigger_score_recalculation
from app.persistence.db.session import get_db_session
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.models.user import User
from app.persistence.repositories.transaction_repository import SaleRepository
from app.schemas.common import MessageResponse
from app.schemas.transaction import CreateSaleRequest, SaleEntryResponse, UpdateSaleRequest

router = APIRouter()


@router.get("", response_model=list[SaleEntryResponse], summary="List sales entries")
async def list_sales(
    from_date: date | None = Query(default=None),
    to_date: date | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> list[SaleEntry]:
    repo = SaleRepository(session)
    return await repo.list_by_tenant(
        tenant.id, from_date=from_date, to_date=to_date, limit=limit, offset=offset
    )


@router.post(
    "",
    response_model=SaleEntryResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new sale",
)
async def create_sale(
    body: CreateSaleRequest,
    tenant: Tenant = Depends(get_current_tenant),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> SaleEntry:
    repo = SaleRepository(session)
    entry = SaleEntry(
        tenant_id=tenant.id,
        amount=body.amount,
        quantity=body.quantity,
        transaction_date=body.transaction_date,
        payment_method=body.payment_method,
        product_id=body.product_id,
        notes=body.notes,
    )
    saved = await repo.save(entry)
    # Trigger async score recalculation (non-blocking)
    trigger_score_recalculation.delay(str(tenant.id), "sale_entry_created")
    return saved


@router.get("/{sale_id}", response_model=SaleEntryResponse, summary="Get sale by ID")
async def get_sale(
    sale_id: UUID,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> SaleEntry:
    repo = SaleRepository(session)
    entry = await repo.get_by_id(sale_id, tenant.id)
    if not entry:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sale not found.")
    return entry


@router.patch("/{sale_id}", response_model=SaleEntryResponse, summary="Update a sale entry")
async def update_sale(
    sale_id: UUID,
    body: UpdateSaleRequest,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> SaleEntry:
    repo = SaleRepository(session)
    entry = await repo.get_by_id(sale_id, tenant.id)
    if not entry:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sale not found.")
    if body.amount is not None:
        entry.amount = body.amount
    if body.quantity is not None:
        entry.quantity = body.quantity
    if body.transaction_date is not None:
        entry.transaction_date = body.transaction_date
    if body.payment_method is not None:
        entry.payment_method = body.payment_method
    if body.notes is not None:
        entry.notes = body.notes
    saved = await repo.save(entry)
    trigger_score_recalculation.delay(str(tenant.id), "sale_entry_updated")
    return saved


@router.delete("/{sale_id}", response_model=MessageResponse, summary="Delete a sale entry")
async def delete_sale(
    sale_id: UUID,
    tenant: Tenant = Depends(get_current_tenant),
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> MessageResponse:
    repo = SaleRepository(session)
    entry = await repo.get_by_id(sale_id, tenant.id)
    if not entry:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sale not found.")
    await repo.delete(entry)
    trigger_score_recalculation.delay(str(tenant.id), "sale_entry_deleted")
    return MessageResponse(message="Sale entry deleted.")
