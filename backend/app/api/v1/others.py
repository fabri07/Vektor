"""Sección "Otros" — bandeja de revisión de datos no clasificados.

Todo lo que llegó por chat/ingesta/reanálisis y no se clasificó como
venta/gasto/producto queda en ``unclassified_records``. Desde acá el tenant:
  - lista lo pendiente (`GET /others`),
  - lo importa como venta/gasto/producto (`POST /others/{id}/reclassify`,
    valida con los mismos schemas que los endpoints de creación), o
  - lo descarta (`POST /others/{id}/dismiss`).
"""

import uuid
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_tenant, require_role
from app.api.v1.expenses import _apply_category_label
from app.api.v1.products import _tenant_business_type
from app.application.services.score_trigger_service import trigger_score_recalculation
from app.domain.expense_categories import (
    EXPENSE_CATEGORY_LABELS_ES,
    classify_expense_with_vertical,
    normalize_expense_category,
)
from app.domain.product_categories import (
    PRODUCT_CATEGORY_LABELS,
    _resolve_vertical,
    normalize_product_category,
)
from app.persistence.db.session import get_db_session
from app.persistence.models.customer import Customer
from app.persistence.models.product import Product
from app.persistence.models.supplier import Supplier
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.persistence.models.unclassified_record import (
    UNCLASSIFIED_STATUS_DISMISSED,
    UNCLASSIFIED_STATUS_IMPORTED,
    UNCLASSIFIED_STATUS_PENDING,
    UnclassifiedRecord,
)
from app.persistence.models.user import User
from app.schemas.common import MessageResponse
from app.schemas.customer import CreateCustomerRequest
from app.schemas.product import CreateProductRequest
from app.schemas.supplier import CreateSupplierRequest
from app.schemas.transaction import CreateExpenseRequest, CreateSaleRequest

router = APIRouter()


class UnclassifiedRecordResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    uploaded_file_id: UUID | None
    source: str
    context_label: str | None
    headers: list[str] | None
    row_data: dict[str, Any]
    suggested_entity: str | None
    # Categoría recomendada computada desde la fila cruda con los normalizadores
    # canónicos del domain (catálogo de gastos o de productos según el destino).
    suggested_category: str | None = None
    suggested_category_label: str | None = None
    status: str
    created_at: datetime


class ReclassifyRequest(BaseModel):
    entity_type: Literal["sale", "expense", "product", "customer", "supplier"]
    # Campos del registro a crear; se validan con el schema de creación
    # correspondiente (CreateSaleRequest / CreateExpenseRequest / CreateProductRequest /
    # CreateCustomerRequest / CreateSupplierRequest).
    fields: dict[str, Any]


class BulkImportRequest(BaseModel):
    # None = ventas y gastos sugeridos. Los sugeridos como producto nunca
    # entran en el bulk (necesitan campos que la fila suelta no garantiza).
    entity_type: Literal["sale", "expense"] | None = None


class BulkImportResponse(BaseModel):
    imported_sales: int
    imported_expenses: int
    skipped: int


@router.get(
    "",
    response_model=list[UnclassifiedRecordResponse],
    summary="Registros sin clasificar (bandeja Otros)",
)
async def list_unclassified(
    record_status: str = Query(default=UNCLASSIFIED_STATUS_PENDING, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> list[UnclassifiedRecordResponse]:
    # Lazy import: ingestion_import_service importa schemas/transaction y crearía
    # un ciclo a nivel módulo (mismo criterio que bulk_import_unclassified).
    from app.application.services.ingestion_import_service import (  # noqa: PLC0415
        _row_val_categoria,
    )

    q = (
        select(UnclassifiedRecord)
        .where(
            UnclassifiedRecord.tenant_id == tenant.tenant_id,
            UnclassifiedRecord.status == record_status,
        )
        .order_by(UnclassifiedRecord.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(q)
    records = list(result.scalars().all())

    vertical = await _tenant_business_type(session, tenant.tenant_id)
    responses: list[UnclassifiedRecordResponse] = []
    for rec in records:
        resp = UnclassifiedRecordResponse.model_validate(rec)
        raw = _row_val_categoria(rec.row_data or {})
        raw_s = str(raw).strip() if raw not in (None, "") else None
        if raw_s and rec.suggested_entity == "expense":
            code, custom_label, _ = classify_expense_with_vertical(raw_s, vertical)
            resp.suggested_category = code
            resp.suggested_category_label = custom_label or EXPENSE_CATEGORY_LABELS_ES.get(
                code, code
            )
        elif raw_s and rec.suggested_entity == "product":
            code, custom_label = normalize_product_category(raw_s, vertical)
            resp.suggested_category = code
            resp.suggested_category_label = custom_label or PRODUCT_CATEGORY_LABELS[
                _resolve_vertical(vertical)
            ].get(code, code)
        responses.append(resp)
    return responses


@router.get("/count", summary="Cantidad de registros pendientes en Otros")
async def count_unclassified(
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, int]:
    result = await session.execute(
        select(func.count(UnclassifiedRecord.id)).where(
            UnclassifiedRecord.tenant_id == tenant.tenant_id,
            UnclassifiedRecord.status == UNCLASSIFIED_STATUS_PENDING,
        )
    )
    return {"pending": int(result.scalar_one() or 0)}


async def _get_pending_record(
    session: AsyncSession, tenant_id: uuid.UUID, record_id: UUID
) -> UnclassifiedRecord:
    result = await session.execute(
        select(UnclassifiedRecord).where(
            UnclassifiedRecord.id == record_id,
            UnclassifiedRecord.tenant_id == tenant_id,
        )
    )
    record = result.scalar_one_or_none()
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Registro no encontrado."
        )
    if record.status != UNCLASSIFIED_STATUS_PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="El registro ya fue importado o descartado.",
        )
    return record


@router.post(
    "/{record_id}/reclassify",
    response_model=MessageResponse,
    summary="Importar un registro de Otros como venta/gasto/producto",
)
async def reclassify_record(
    record_id: UUID,
    body: ReclassifyRequest,
    tenant: Tenant = Depends(get_current_tenant),
    _: User = Depends(require_role("OWNER", "ADMIN")),
    session: AsyncSession = Depends(get_db_session),
) -> MessageResponse:
    record = await _get_pending_record(session, tenant.tenant_id, record_id)

    try:
        if body.entity_type == "sale":
            sale_req = CreateSaleRequest(**body.fields)
            session.add(
                SaleEntry(
                    tenant_id=tenant.tenant_id,
                    amount=sale_req.amount,
                    quantity=sale_req.quantity,
                    transaction_date=sale_req.transaction_date,
                    payment_method=sale_req.payment_method,
                    product_id=sale_req.product_id,
                    notes=sale_req.notes,
                    custom_fields=sale_req.custom_fields,
                    provenance="REAL",
                )
            )
            label = "venta"
        elif body.entity_type == "expense":
            # Normalizar la categoría libre antes de validar contra el catálogo.
            raw_fields = dict(body.fields)
            code, cat_label = normalize_expense_category(raw_fields.get("category"))
            raw_fields["category"] = code
            if cat_label and not raw_fields.get("category_label"):
                raw_fields["category_label"] = cat_label
            exp_req = CreateExpenseRequest(**raw_fields)
            custom_fields = _apply_category_label(
                exp_req.custom_fields, exp_req.category, exp_req.category_label
            )
            session.add(
                ExpenseEntry(
                    tenant_id=tenant.tenant_id,
                    amount=exp_req.amount,
                    category=exp_req.category,
                    expense_type=exp_req.expense_type,
                    transaction_date=exp_req.expense_date,
                    description=exp_req.description,
                    is_recurring=exp_req.is_recurring,
                    payment_method=exp_req.payment_method,
                    supplier_name=exp_req.supplier_name,
                    notes=exp_req.notes,
                    custom_fields=custom_fields,
                    provenance="REAL",
                )
            )
            label = "gasto"
        elif body.entity_type == "customer":
            cust_req = CreateCustomerRequest(**body.fields)
            session.add(Customer(tenant_id=tenant.tenant_id, **cust_req.model_dump()))
            label = "cliente"
        elif body.entity_type == "supplier":
            sup_req = CreateSupplierRequest(**body.fields)
            session.add(Supplier(tenant_id=tenant.tenant_id, **sup_req.model_dump()))
            label = "proveedor"
        else:  # "product"
            prod_req = CreateProductRequest(**body.fields)
            data = prod_req.model_dump()
            # Misma normalización que POST /products (catálogo del vertical).
            if data.get("category"):
                business_type = await _tenant_business_type(session, tenant.tenant_id)
                code_p, label_p = normalize_product_category(data["category"], business_type)
                data["category"] = code_p
                if label_p:
                    data["custom_fields"] = {
                        **(data.get("custom_fields") or {}),
                        "category_label": label_p,
                    }
            session.add(Product(tenant_id=tenant.tenant_id, **data))
            label = "producto"
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.errors(),
        ) from exc

    record.status = UNCLASSIFIED_STATUS_IMPORTED
    record.resolved_at = datetime.now(UTC)
    await session.flush()
    trigger_score_recalculation.delay(str(tenant.tenant_id), "unclassified_reclassified")
    return MessageResponse(message=f"Registro importado como {label}.")


@router.post(
    "/bulk-import",
    response_model=BulkImportResponse,
    summary="Importar en lote los registros de Otros sugeridos como venta/gasto",
)
async def bulk_import_records(
    body: BulkImportRequest,
    tenant: Tenant = Depends(get_current_tenant),
    _: User = Depends(require_role("OWNER", "ADMIN")),
    session: AsyncSession = Depends(get_db_session),
) -> BulkImportResponse:
    """Importa cada PENDING con `suggested_entity` venta/gasto cuya fila cruda
    parsea (fecha + monto), con las mismas normalizaciones canónicas que el
    import de archivos. Lo que no parsea queda PENDING para el modal manual."""
    from app.application.services.ingestion_import_service import (  # noqa: PLC0415
        bulk_import_unclassified,
    )

    counts = await bulk_import_unclassified(session, tenant.tenant_id, body.entity_type)
    if counts["imported_sales"] or counts["imported_expenses"]:
        trigger_score_recalculation.delay(str(tenant.tenant_id), "unclassified_bulk_import")
    return BulkImportResponse(**counts)


@router.post(
    "/{record_id}/dismiss",
    response_model=MessageResponse,
    summary="Descartar un registro de Otros",
)
async def dismiss_record(
    record_id: UUID,
    tenant: Tenant = Depends(get_current_tenant),
    _: User = Depends(require_role("OWNER", "ADMIN")),
    session: AsyncSession = Depends(get_db_session),
) -> MessageResponse:
    record = await _get_pending_record(session, tenant.tenant_id, record_id)
    record.status = UNCLASSIFIED_STATUS_DISMISSED
    record.resolved_at = datetime.now(UTC)
    await session.flush()
    return MessageResponse(message="Registro descartado.")
