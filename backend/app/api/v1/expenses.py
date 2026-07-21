"""Expense entry endpoints."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import (
    ensure_tenant_not_under_maintenance,
    get_current_tenant,
    require_modify_access,
    require_role,
)
from app.application.services import tenant_categories_service
from app.application.services.idempotency import claim_idempotency_key
from app.application.services.score_trigger_service import trigger_score_recalculation
from app.domain.expense_categories import infer_expense_type
from app.persistence.db.session import get_db_session
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry
from app.persistence.models.user import User
from app.persistence.repositories.supplier_repository import SupplierRepository
from app.persistence.repositories.transaction_repository import ExpenseRepository
from app.schemas.common import MessageResponse
from app.schemas.transaction import (
    CreateExpenseRequest,
    DateRangeResponse,
    ExpenseEntryResponse,
    ExpenseSummaryResponse,
    ProfitWithdrawalRequest,
    UpdateExpenseRequest,
)

router = APIRouter()

VOID_REASON_MANUAL = "MANUAL_ADMIN_VOID"


def _apply_category_label(
    custom_fields: dict[str, object] | None,
    category: str | None,
    label: str | None,
) -> dict[str, object]:
    """FASE 3.1: guarda/limpia el label de categoría "Otro" en custom_fields.

    Si category == OTHER y hay label (trim, ≤50) → custom_fields["category_label"].
    Si la categoría no es OTHER → se quita el label (evita labels huérfanos).
    """
    cf: dict[str, object] = dict(custom_fields or {})
    if category == "OTHER":
        if label and label.strip():
            cf["category_label"] = label.strip()[:50]
    else:
        cf.pop("category_label", None)
    return cf


def _expense_snapshot(entry: ExpenseEntry) -> dict[str, object]:
    return {
        "id": str(entry.id),
        "amount": str(entry.amount),
        "category": entry.category,
        "transaction_date": str(entry.transaction_date),
        "description": entry.description,
        "is_recurring": entry.is_recurring,
        "payment_method": entry.payment_method,
        "supplier_name": entry.supplier_name,
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
                "record_type": "expense",
                "record_id": before["id"],
                "before": before,
                "after": after,
                "source": "ui",
            },
            triggered_by="ui:data_records",
            actor_user_id=user_id,
            context={"endpoint": "expenses"},
            created_at=datetime.now(UTC),
        )
    )


@router.get(
    "/summary",
    response_model=ExpenseSummaryResponse,
    summary="Last-30-day expense summary",
)
async def expenses_summary(
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> ExpenseSummaryResponse:
    repo = ExpenseRepository(session)
    to_date = date.today()
    from_date = to_date - timedelta(days=30)
    total = await repo.total_expenses(tenant.tenant_id, from_date=from_date, to_date=to_date)
    count = await repo.count_by_date_range(tenant.tenant_id, from_date=from_date, to_date=to_date)
    return ExpenseSummaryResponse(
        total_ars=Decimal(str(total)),
        entry_count=count,
        period_covered=f"{from_date} al {to_date}",
    )


@router.get(
    "/date-range",
    response_model=DateRangeResponse,
    summary="First/last expense date for the tenant",
)
async def expenses_date_range(
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> DateRangeResponse:
    repo = ExpenseRepository(session)
    min_date, max_date = await repo.date_range(tenant.tenant_id)
    return DateRangeResponse(min_date=min_date, max_date=max_date)


@router.get("", response_model=list[ExpenseEntryResponse], summary="List expense entries")
async def list_expenses(
    from_date: date | None = Query(default=None),
    to_date: date | None = Query(default=None),
    category: str | None = Query(default=None),
    expense_type: str | None = Query(default=None, pattern=r"^(OPEX|COGS)$"),
    supplier_id: UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> list[ExpenseEntry]:
    if from_date is None:
        from_date = date.today() - timedelta(days=30)
    repo = ExpenseRepository(session)
    return await repo.list_by_tenant(
        tenant.tenant_id,
        from_date=from_date,
        to_date=to_date,
        category=category,
        expense_type=expense_type,
        supplier_id=supplier_id,
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
    # F3 review final: la auth (rol) va ANTES que el guard 423 — mismo orden que
    # products/others/suppliers (ver create_product).
    _: User = Depends(require_role("OWNER", "ADMIN")),
    _maintenance_guard: None = Depends(ensure_tenant_not_under_maintenance),
    session: AsyncSession = Depends(get_db_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ExpenseEntry:
    # Idempotencia opcional: si llega la key, reclamarla ANTES de crear. Comparte
    # transacción con la creación (commit único en get_db_session), por lo que si
    # la creación falla el claim se revierte y un reintento futuro puede entrar.
    if idempotency_key is not None and not await claim_idempotency_key(
        session, tenant.tenant_id, idempotency_key, "IDEMPOTENT_POST_EXPENSE"
    ):
        raise HTTPException(status_code=409, detail={"code": "DUPLICATE_IDEMPOTENT"})
    # El supplier_id debe pertenecer al tenant (la FK sola no lo garantiza).
    # Si hay proveedor y no se dio supplier_name, lo denormalizamos desde la
    # entidad para que rankings/análisis (que agrupan por supplier_name) lo vean.
    supplier_name = body.supplier_name
    if body.supplier_id is not None:
        supplier = await SupplierRepository(session).get_by_id(body.supplier_id, tenant.tenant_id)
        if supplier is None:
            raise HTTPException(status_code=400, detail="Supplier not found for this tenant.")
        if not (supplier_name and supplier_name.strip()):
            supplier_name = supplier.name
    repo = ExpenseRepository(session)
    # FASE 3.1: categoría "Otro" editable — el label libre va a custom_fields;
    # category sigue siendo OTHER (reportes/agregaciones intactos).
    custom_fields = _apply_category_label(body.custom_fields, body.category, body.category_label)
    # Categoría "Otros" con label → persistir por tenant para que reaparezca en
    # futuros desplegables (sin tabla ni pantalla nueva; vive en custom_fields).
    if body.category == "OTHER" and body.category_label and body.category_label.strip():
        await tenant_categories_service.add_expense_category(
            session, tenant.tenant_id, body.category_label
        )
    entry = ExpenseEntry(
        tenant_id=tenant.tenant_id,
        amount=body.amount,
        category=body.category,
        # Discriminador canónico COGS/OPEX: un valor explícito del usuario gana; si no
        # eligió (None), infiere por categoría (mercadería/INVENTORY → COGS). Antes se
        # persistía el default "OPEX" y una compra de mercadería manual quedaba mal.
        expense_type=infer_expense_type(body.category, explicit=body.expense_type),
        transaction_date=body.expense_date,
        description=body.description,
        is_recurring=body.is_recurring,
        payment_method=body.payment_method,
        supplier_name=supplier_name,
        supplier_id=body.supplier_id,
        notes=body.notes,
        custom_fields=custom_fields,
    )
    saved = await repo.save(entry)
    trigger_score_recalculation.delay(str(tenant.tenant_id), "expense_entry_created")
    return saved


@router.post(
    "/profit-withdrawal",
    response_model=ExpenseEntryResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register an early profit withdrawal (owner salary) as a PAYROLL expense",
)
async def create_profit_withdrawal(
    body: ProfitWithdrawalRequest,
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(require_modify_access),
    session: AsyncSession = Depends(get_db_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ExpenseEntry:
    """Retiro de ganancias anticipadas: gasto categoría PAYROLL/OPEX. El monto lo
    fija el usuario (no se calcula). Protegido por PIN."""
    if idempotency_key is not None and not await claim_idempotency_key(
        session, tenant.tenant_id, idempotency_key, "IDEMPOTENT_POST_PROFIT_WITHDRAWAL"
    ):
        raise HTTPException(status_code=409, detail={"code": "DUPLICATE_IDEMPOTENT"})
    repo = ExpenseRepository(session)
    entry = ExpenseEntry(
        tenant_id=tenant.tenant_id,
        amount=body.amount,
        category="PAYROLL",
        expense_type="OPEX",
        transaction_date=body.withdrawal_date,
        description="Retiro de ganancias anticipadas",
        is_recurring=False,
        payment_method=body.payment_method,
        notes=body.notes,
        custom_fields={"profit_withdrawal": True},
    )
    saved = await repo.save(entry)
    _audit_data_change(
        session,
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        decision_type="DATA_RECORD_CREATED",
        before=_expense_snapshot(saved),
        after=_expense_snapshot(saved),
    )
    trigger_score_recalculation.delay(str(tenant.tenant_id), "profit_withdrawal_created")
    return saved


@router.get(
    "/custom-categories",
    summary="Categorías de gasto personalizadas del tenant",
)
async def list_custom_expense_categories(
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> list[str]:
    return await tenant_categories_service.list_expense_categories(session, tenant.tenant_id)


@router.get("/{expense_id}", response_model=ExpenseEntryResponse, summary="Get expense by ID")
async def get_expense(
    expense_id: UUID,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> ExpenseEntry:
    repo = ExpenseRepository(session)
    entry = await repo.get_by_id(expense_id, tenant.tenant_id)
    if not entry:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Expense not found.")
    return entry


@router.patch("/{expense_id}", response_model=ExpenseEntryResponse, summary="Update an expense")
async def update_expense(
    expense_id: UUID,
    body: UpdateExpenseRequest,
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(require_modify_access),
    session: AsyncSession = Depends(get_db_session),
) -> ExpenseEntry:
    repo = ExpenseRepository(session)
    entry = await repo.get_by_id(expense_id, tenant.tenant_id)
    if not entry:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Expense not found.")
    before = _expense_snapshot(entry)
    if body.amount is not None:
        entry.amount = body.amount
    if body.category is not None:
        entry.category = body.category
    if body.expense_type is not None:
        entry.expense_type = body.expense_type
    if body.expense_date is not None:
        entry.transaction_date = body.expense_date
    if body.description is not None:
        entry.description = body.description
    if body.is_recurring is not None:
        entry.is_recurring = body.is_recurring
    if body.supplier_name is not None:
        entry.supplier_name = body.supplier_name
    # supplier_id: usar model_fields_set para poder limpiarlo (null) explícitamente.
    if "supplier_id" in body.model_fields_set:
        if body.supplier_id is not None:
            supplier = await SupplierRepository(session).get_by_id(
                body.supplier_id, tenant.tenant_id
            )
            if supplier is None:
                raise HTTPException(status_code=400, detail="Supplier not found for this tenant.")
            entry.supplier_id = body.supplier_id
            # Denormalizar nombre si quedó vacío (mantiene análisis por supplier_name).
            if not (entry.supplier_name and entry.supplier_name.strip()):
                entry.supplier_name = supplier.name
        else:
            entry.supplier_id = None
    if body.notes is not None:
        entry.notes = body.notes
    if body.custom_fields is not None:
        entry.custom_fields = body.custom_fields
    # FASE 3.1: re-aplicar label de "Otro" si cambió la categoría o el label.
    if body.category_label is not None or body.category is not None:
        entry.custom_fields = _apply_category_label(
            entry.custom_fields, entry.category, body.category_label
        )
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
        after=_expense_snapshot(saved),
    )
    trigger_score_recalculation.delay(str(tenant.tenant_id), "expense_entry_updated")
    return saved


@router.delete("/{expense_id}", response_model=MessageResponse, summary="Delete an expense entry")
async def delete_expense(
    expense_id: UUID,
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(require_modify_access),
    session: AsyncSession = Depends(get_db_session),
) -> MessageResponse:
    repo = ExpenseRepository(session)
    entry = await repo.get_by_id(expense_id, tenant.tenant_id)
    if not entry:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Expense not found.")
    before = _expense_snapshot(entry)
    entry.voided_at = datetime.now(UTC)
    entry.void_reason = VOID_REASON_MANUAL
    await repo.save(entry)
    _audit_data_change(
        session,
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        decision_type="DATA_RECORD_VOIDED",
        before=before,
        after=_expense_snapshot(entry),
    )
    trigger_score_recalculation.delay(str(tenant.tenant_id), "expense_entry_deleted")
    return MessageResponse(message="Expense entry voided.")
