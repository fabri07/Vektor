"""Sales entry endpoints."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_tenant, require_role
from app.application.services.customer_sentinel import (
    resolve_or_create_local_sentinel,
)
from app.application.services.idempotency import claim_idempotency_key
from app.application.services.score_trigger_service import trigger_score_recalculation
from app.persistence.db.session import get_db_session
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.models.user import User
from app.persistence.repositories.customer_repository import CustomerRepository
from app.persistence.repositories.transaction_repository import SaleRepository
from app.schemas.common import MessageResponse
from app.schemas.transaction import (
    BulkSaleRequest,
    CreateSaleRequest,
    DateRangeResponse,
    SaleEntryResponse,
    SaleSummaryResponse,
    UpdateSaleRequest,
)

router = APIRouter()

VOID_REASON_MANUAL = "MANUAL_ADMIN_VOID"
# Fiado: venta a cuenta corriente. Exige un cliente REAL (nunca el sentinela
# "Local"): saber a quién se le fía es el punto. Espejo en el frontend.
FIADO_PAYMENT_METHOD = "account"
_FIADO_REQUIRES_CUSTOMER = "El fiado requiere un cliente registrado (no puede ser 'Local')."


def _sale_snapshot(entry: SaleEntry) -> dict[str, object]:
    return {
        "id": str(entry.id),
        "amount": str(entry.amount),
        "quantity": entry.quantity,
        "transaction_date": str(entry.transaction_date),
        "payment_method": entry.payment_method,
        "product_id": str(entry.product_id) if entry.product_id else None,
        "customer_id": str(entry.customer_id) if entry.customer_id else None,
        "notes": entry.notes,
        "custom_fields": entry.custom_fields,
        "voided_at": entry.voided_at.isoformat() if entry.voided_at else None,
        "void_reason": entry.void_reason,
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
                "record_type": "sale",
                "record_id": before["id"],
                "before": before,
                "after": after,
                "source": "ui",
            },
            triggered_by="ui:data_records",
            actor_user_id=user_id,
            context={"endpoint": "sales"},
            created_at=datetime.now(UTC),
        )
    )


@router.get("/summary", response_model=SaleSummaryResponse, summary="Last-30-day sales summary")
async def sales_summary(
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> SaleSummaryResponse:
    repo = SaleRepository(session)
    to_date = date.today()
    from_date = to_date - timedelta(days=30)
    total = await repo.total_revenue(tenant.tenant_id, from_date=from_date, to_date=to_date)
    count = await repo.count_by_date_range(tenant.tenant_id, from_date=from_date, to_date=to_date)
    return SaleSummaryResponse(
        total_ars=Decimal(str(total)),
        entry_count=count,
        period_covered=f"{from_date} al {to_date}",
    )


@router.get(
    "/date-range",
    response_model=DateRangeResponse,
    summary="First/last sale date for the tenant",
)
async def sales_date_range(
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> DateRangeResponse:
    repo = SaleRepository(session)
    min_date, max_date = await repo.date_range(tenant.tenant_id)
    return DateRangeResponse(min_date=min_date, max_date=max_date)


@router.get("", response_model=list[SaleEntryResponse], summary="List sales entries")
async def list_sales(
    from_date: date | None = Query(default=None),
    to_date: date | None = Query(default=None),
    customer_id: UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> list[SaleEntry]:
    if from_date is None:
        from_date = date.today() - timedelta(days=30)
    repo = SaleRepository(session)
    return await repo.list_by_tenant(
        tenant.tenant_id,
        from_date=from_date,
        to_date=to_date,
        customer_id=customer_id,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/bulk",
    response_model=list[SaleEntryResponse],
    status_code=status.HTTP_201_CREATED,
    summary="Bulk-load sales for a period",
)
async def bulk_create_sales(
    body: BulkSaleRequest,
    tenant: Tenant = Depends(get_current_tenant),
    _: User = Depends(require_role("OWNER", "ADMIN")),
    session: AsyncSession = Depends(get_db_session),
) -> list[SaleEntry]:
    repo = SaleRepository(session)
    # Carga masiva sin cliente (siempre "cash", nunca fiado): todas las entries van
    # al sentinela "Local" para que ninguna venta quede sin cliente.
    local_id = await resolve_or_create_local_sentinel(session, tenant.tenant_id)
    if body.entries:
        entries = [
            SaleEntry(
                tenant_id=tenant.tenant_id,
                amount=item.amount_ars,
                quantity=item.quantity,
                transaction_date=body.period_date,
                payment_method="cash",
                product_id=item.product_id,
                customer_id=local_id,
            )
            for item in body.entries
        ]
    else:
        entries = [
            SaleEntry(
                tenant_id=tenant.tenant_id,
                amount=body.total_amount_ars,
                quantity=1,
                transaction_date=body.period_date,
                payment_method="cash",
                notes=body.period_type,
                customer_id=local_id,
            )
        ]
    saved = await repo.bulk_save(entries)
    trigger_score_recalculation.delay(str(tenant.tenant_id), "sales_bulk_created")
    return saved


@router.post(
    "",
    response_model=SaleEntryResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new sale",
)
async def create_sale(
    body: CreateSaleRequest,
    tenant: Tenant = Depends(get_current_tenant),
    _: User = Depends(require_role("OWNER", "ADMIN")),
    session: AsyncSession = Depends(get_db_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> SaleEntry:
    # Idempotencia opcional: si llega la key, reclamarla ANTES de crear. Comparte
    # transacción con la creación (commit único en get_db_session), por lo que si
    # la creación falla el claim se revierte y un reintento futuro puede entrar.
    if idempotency_key is not None and not await claim_idempotency_key(
        session, tenant.tenant_id, idempotency_key, "IDEMPOTENT_POST_SALE"
    ):
        raise HTTPException(status_code=409, detail={"code": "DUPLICATE_IDEMPOTENT"})
    is_fiado = body.payment_method == FIADO_PAYMENT_METHOD
    if body.customer_id is not None:
        # El customer_id debe pertenecer al tenant (la FK sola no lo garantiza).
        customer = await CustomerRepository(session).get_by_id(body.customer_id, tenant.tenant_id)
        if customer is None:
            raise HTTPException(status_code=400, detail="Customer not found for this tenant.")
        if is_fiado and customer.is_sentinel:
            raise HTTPException(status_code=400, detail=_FIADO_REQUIRES_CUSTOMER)
        customer_id = body.customer_id
    elif is_fiado:
        # Fiado sin cliente → rechazo (no se permite fiar a "Local").
        raise HTTPException(status_code=400, detail=_FIADO_REQUIRES_CUSTOMER)
    else:
        # Venta sin cliente registrado → sentinela "Local" (nunca queda NULL).
        customer_id = await resolve_or_create_local_sentinel(session, tenant.tenant_id)
    repo = SaleRepository(session)
    entry = SaleEntry(
        tenant_id=tenant.tenant_id,
        amount=body.amount,
        quantity=body.quantity,
        transaction_date=body.transaction_date,
        payment_method=body.payment_method,
        product_id=body.product_id,
        customer_id=customer_id,
        notes=body.notes,
        custom_fields=body.custom_fields,
    )
    saved = await repo.save(entry)
    trigger_score_recalculation.delay(str(tenant.tenant_id), "sale_entry_created")
    return saved


@router.get("/{sale_id}", response_model=SaleEntryResponse, summary="Get sale by ID")
async def get_sale(
    sale_id: UUID,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> SaleEntry:
    repo = SaleRepository(session)
    entry = await repo.get_by_id(sale_id, tenant.tenant_id)
    if not entry:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sale not found.")
    return entry


@router.patch("/{sale_id}", response_model=SaleEntryResponse, summary="Update a sale entry")
async def update_sale(
    sale_id: UUID,
    body: UpdateSaleRequest,
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(require_role("OWNER", "ADMIN")),
    session: AsyncSession = Depends(get_db_session),
) -> SaleEntry:
    repo = SaleRepository(session)
    entry = await repo.get_by_id(sale_id, tenant.tenant_id)
    if not entry:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sale not found.")
    before = _sale_snapshot(entry)
    if body.amount is not None:
        entry.amount = body.amount
    if body.quantity is not None:
        entry.quantity = body.quantity
    if body.transaction_date is not None:
        entry.transaction_date = body.transaction_date
    if body.payment_method is not None:
        entry.payment_method = body.payment_method
    if "product_id" in body.model_fields_set:
        entry.product_id = body.product_id
    # ``payment_method`` ya quedó en su valor final arriba → calculamos fiado sobre él.
    is_fiado = entry.payment_method == FIADO_PAYMENT_METHOD
    if "customer_id" in body.model_fields_set:
        if body.customer_id is not None:
            customer = await CustomerRepository(session).get_by_id(
                body.customer_id, tenant.tenant_id
            )
            if customer is None:
                raise HTTPException(status_code=400, detail="Customer not found for this tenant.")
            if is_fiado and customer.is_sentinel:
                raise HTTPException(status_code=400, detail=_FIADO_REQUIRES_CUSTOMER)
            entry.customer_id = body.customer_id
        elif is_fiado:
            raise HTTPException(status_code=400, detail=_FIADO_REQUIRES_CUSTOMER)
        else:
            # Limpiar el cliente → sentinela "Local" (la venta nunca queda sin cliente).
            entry.customer_id = await resolve_or_create_local_sentinel(
                session, tenant.tenant_id
            )
    elif is_fiado:
        # El cliente no cambió, pero la venta pasó a fiado: exigir cliente real.
        current = (
            await CustomerRepository(session).get_by_id(entry.customer_id, tenant.tenant_id)
            if entry.customer_id is not None
            else None
        )
        if current is None or current.is_sentinel:
            raise HTTPException(status_code=400, detail=_FIADO_REQUIRES_CUSTOMER)
    if body.notes is not None:
        entry.notes = body.notes
    if body.custom_fields is not None:
        entry.custom_fields = body.custom_fields
    # Relectura de archivos: si esta fila vino de un import, marcarla como editada
    # a mano para que la re-importación no la pise.
    if entry.source_upload_id is not None:
        entry.has_user_edits = True
    saved = await repo.save(entry)
    _audit_data_change(
        session,
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        decision_type="DATA_RECORD_UPDATED",
        before=before,
        after=_sale_snapshot(saved),
    )
    trigger_score_recalculation.delay(str(tenant.tenant_id), "sale_entry_updated")
    return saved


@router.delete("/{sale_id}", response_model=MessageResponse, summary="Delete a sale entry")
async def delete_sale(
    sale_id: UUID,
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(require_role("OWNER", "ADMIN")),
    session: AsyncSession = Depends(get_db_session),
) -> MessageResponse:
    repo = SaleRepository(session)
    entry = await repo.get_by_id(sale_id, tenant.tenant_id)
    if not entry:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sale not found.")
    before = _sale_snapshot(entry)
    entry.voided_at = datetime.now(UTC)
    entry.void_reason = VOID_REASON_MANUAL
    await repo.save(entry)
    _audit_data_change(
        session,
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        decision_type="DATA_RECORD_VOIDED",
        before=before,
        after=_sale_snapshot(entry),
    )
    trigger_score_recalculation.delay(str(tenant.tenant_id), "sale_entry_deleted")
    return MessageResponse(message="Sale entry voided.")
