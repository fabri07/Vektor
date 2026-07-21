"""Sección "Otros" — bandeja de revisión de datos no clasificados.

Todo lo que llegó por chat/ingesta/reanálisis y no se clasificó como
venta/gasto/producto queda en ``unclassified_records``. Desde acá el tenant:
  - lista lo pendiente (`GET /others`),
  - lo importa como venta/gasto/producto (`POST /others/{id}/reclassify`,
    valida con los mismos schemas que los endpoints de creación), o
  - lo descarta (`POST /others/{id}/dismiss`).
"""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import ensure_tenant_not_under_maintenance, get_current_tenant, require_role
from app.api.v1.expenses import _apply_category_label
from app.api.v1.products import (
    _duplicate_identity_conflict,
    _find_active_product_by_identity,
    _identity_conflict_from_db,
    _tenant_business_type,
)
from app.application.services import maintenance_lock_service, stock_service
from app.application.services.product_identity import (
    ProductIdentityConflictError,
    product_identity_guard,
)
from app.application.services.score_trigger_service import trigger_score_recalculation
from app.domain.expense_categories import (
    EXPENSE_CATEGORY_LABELS_ES,
    classify_expense_with_vertical,
    infer_expense_type,
    normalize_expense_category,
)
from app.domain.product_categories import (
    PRODUCT_CATEGORY_LABELS,
    _resolve_vertical,
    normalize_product_category,
)
from app.domain.product_completion import recompute_requires_completion
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
from app.persistence.repositories.product_repository import ProductRepository
from app.schemas.common import MessageResponse
from app.schemas.customer import CreateCustomerRequest
from app.schemas.product import CreateProductRequest, UpdateProductRequest
from app.schemas.supplier import CreateSupplierRequest
from app.schemas.transaction import (
    PAYMENT_METHOD_PATTERN,
    CreateExpenseRequest,
    CreateSaleRequest,
)

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
    # F2-T2b: candidatos de match para una fila de producto ambigua/en conflicto
    # (forma ``{id, matched_by, name, sku, barcode}``). El frontend los ofrece
    # para VINCULAR a un producto existente (POST reclassify con
    # ``target_product_id``) en vez de crear un duplicado. None fuera de ese caso.
    match_candidates: list[dict[str, Any]] | None = None
    status: str
    created_at: datetime


class ReclassifyRequest(BaseModel):
    entity_type: Literal["sale", "expense", "product", "customer", "supplier"]
    # Campos del registro a crear; se validan con el schema de creación
    # correspondiente (CreateSaleRequest / CreateExpenseRequest / CreateProductRequest /
    # CreateCustomerRequest / CreateSupplierRequest).
    fields: dict[str, Any]
    # F2-T2b: solo para entity_type="product". Si viene, en vez de crear un
    # producto nuevo se VINCULA el registro a este producto ya existente (elegido
    # entre los `match_candidates` que armó T2 para un producto ambiguo). El id
    # SIEMPRE se re-valida contra el tenant en el handler — nunca se confía en el
    # id que manda el cliente.
    target_product_id: UUID | None = None


class ResolvePurchaseRequest(BaseModel):
    target_product_id: UUID
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    quantity: int = Field(gt=0)
    unit_cost: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    transaction_date: datetime
    payment_method: str = Field(pattern=PAYMENT_METHOD_PATTERN, default="transfer")
    category: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    supplier_name: str | None = Field(default=None, max_length=300)

    @field_validator("transaction_date")
    @classmethod
    def transaction_date_not_future(cls, value: datetime) -> datetime:
        if value.date() > date.today():
            raise ValueError("transaction_date cannot be in the future.")
        return value


class BulkImportRequest(BaseModel):
    # None = ventas y gastos sugeridos. Los sugeridos como producto nunca
    # entran en el bulk (necesitan campos que la fila suelta no garantiza).
    entity_type: Literal["sale", "expense"] | None = None


class BulkImportResponse(BaseModel):
    imported_sales: int
    imported_expenses: int
    skipped: int  # ya importados (idempotencia)
    needs_manual: int = 0  # no parsearon (fecha/monto ilegible) → completar a mano


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
    session: AsyncSession,
    tenant_id: uuid.UUID,
    record_id: UUID,
    *,
    for_update: bool = False,
) -> UnclassifiedRecord:
    query = select(UnclassifiedRecord).where(
            UnclassifiedRecord.id == record_id,
            UnclassifiedRecord.tenant_id == tenant_id,
        )
    if for_update:
        query = query.with_for_update()
    result = await session.execute(query)
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
    # F3-T3 review: auth ANTES que el guard 423 (ver nota en resolve_purchase). Este
    # endpoint puede crear/mutar Product (ramas "product" abajo) — CRITICAL sin
    # tocar del cableado original de F3-T3.
    _: User = Depends(require_role("OWNER", "ADMIN")),
    _maintenance_guard: None = Depends(ensure_tenant_not_under_maintenance),
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
                    # Inferir COGS/OPEX (una fila de mercadería clasificada desde "Otros"
                    # debe quedar COGS); un expense_type explícito del usuario gana.
                    expense_type=infer_expense_type(
                        exp_req.category, explicit=exp_req.expense_type
                    ),
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
        elif body.target_product_id is not None:  # "product", vincular a existente
            # Re-validación (seguridad, crítico): NUNCA confiar en el id que manda
            # el cliente. Se re-carga filtrando por tenant_id + is_active — un id
            # ajeno, inexistente o de un producto ya inactivo se rechaza.
            product_repo = ProductRepository(session)
            target = await product_repo.get_by_id(body.target_product_id, tenant.tenant_id)
            if target is None or not target.is_active:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={"code": "INVALID_TARGET_PRODUCT"},
                )
            # Solo los 3 campos ajustables desde el link (nunca None pisa lo existente:
            # exclude_unset se queda solo con lo que el usuario mandó).
            if "stock_units" in body.fields:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={"code": "STOCK_VIA_LINK_FORBIDDEN"},
                )
            # CRITICAL (F3-T3 review): shared lock ANTES de mutar el producto existente
            # vía setattr (no pasa por ProductRepository.save()) — barrera real contra
            # el dedup, que toma el exclusive.
            await maintenance_lock_service.acquire_write_lock_shared(session, tenant.tenant_id)
            link_updates = UpdateProductRequest(
                **{
                    k: v
                    for k, v in body.fields.items()
                    if k in {"sale_price_ars", "unit_cost_ars"}
                }
            ).model_dump(exclude_unset=True)
            for field_name, value in link_updates.items():
                setattr(target, field_name, value)
            recompute_requires_completion(target)
            record.status = UNCLASSIFIED_STATUS_IMPORTED
            record.resolved_at = datetime.now(UTC)
            await session.flush()
            trigger_score_recalculation.delay(
                str(tenant.tenant_id), "unclassified_reclassified"
            )
            return MessageResponse(message="Registro vinculado al producto existente.")
        else:  # "product", crear nuevo
            # CRITICAL (F3-T3 review): shared lock ANTES de mutar catálogo — el
            # session.add(Product(...)) de abajo NO pasa por ProductRepository.save(),
            # así que no queda cubierto por el acquire de ese chokepoint.
            await maintenance_lock_service.acquire_write_lock_shared(session, tenant.tenant_id)
            prod_req = CreateProductRequest(**body.fields)
            data = prod_req.model_dump()
            existing_product = await _find_active_product_by_identity(
                session, tenant.tenant_id, barcode=data.get("barcode"), sku=data.get("sku")
            )
            if existing_product is not None:
                raise _duplicate_identity_conflict(
                    existing_product, barcode=data.get("barcode"), sku=data.get("sku")
                )
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
            # F5-A: el `_find_active_product_by_identity` de arriba tiene ventana
            # TOCTOU; el guard emite el INSERT dentro del savepoint y traduce la
            # violación del índice único al MISMO 409 que ese pre-check.
            try:
                async with product_identity_guard(
                    session,
                    tenant_id=tenant.tenant_id,
                    barcode=data.get("barcode"),
                    sku=data.get("sku"),
                ):
                    session.add(Product(tenant_id=tenant.tenant_id, **data))
            except ProductIdentityConflictError as conflict:
                # ``_identity_conflict_from_db`` y no ``_duplicate_identity_conflict``:
                # cuando barcode y sku pertenecen a productos DISTINTOS hay que
                # devolver los dos ids. Colapsarlos en uno elegido por prioridad de
                # índice es adivinar, justo en el caso donde el usuario tiene que
                # decidir cuál corresponde.
                raise _identity_conflict_from_db(
                    conflict, barcode=data.get("barcode"), sku=data.get("sku")
                ) from conflict
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
    "/{record_id}/resolve-purchase",
    response_model=MessageResponse,
    summary="Resolver una compra ambigua vinculándola a un producto",
)
async def resolve_purchase(
    record_id: UUID,
    body: ResolvePurchaseRequest,
    tenant: Tenant = Depends(get_current_tenant),
    # F3-T3 review: auth ANTES que el guard 423 — si no, un VIEWER con el tenant
    # lockeado recibiría 423 en vez de 403.
    _: User = Depends(require_role("OWNER", "ADMIN")),
    _maintenance_guard: None = Depends(ensure_tenant_not_under_maintenance),
    session: AsyncSession = Depends(get_db_session),
) -> MessageResponse:
    record = await _get_pending_record(
        session, tenant.tenant_id, record_id, for_update=True
    )
    candidates = record.match_candidates or []
    if record.suggested_entity != "expense" or not candidates:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "NOT_AMBIGUOUS_PURCHASE"},
        )
    candidate_ids = {str(candidate.get("id")) for candidate in candidates if candidate.get("id")}
    if str(body.target_product_id) not in candidate_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "TARGET_NOT_A_CANDIDATE"},
        )

    target = await ProductRepository(session).get_by_id(
        body.target_product_id, tenant.tenant_id
    )
    if target is None or not target.is_active:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_TARGET_PRODUCT"},
        )

    unit_cost = body.unit_cost
    if unit_cost is None:
        unit_cost = (body.amount / body.quantity).quantize(Decimal("0.01"))

    code, category_label = normalize_expense_category(body.category or "INVENTORY")
    supplier_index: dict[str, UUID]
    if body.supplier_name and body.supplier_name.strip():
        from app.application.services.ingestion_import_service import (  # noqa: PLC0415
            _load_supplier_index,
            _resolve_or_create_supplier,
        )

        supplier_index = await _load_supplier_index(session, tenant.tenant_id)
        supplier_id, supplier_name = await _resolve_or_create_supplier(
            session, tenant.tenant_id, body.supplier_name, supplier_index
        )
    else:
        from app.application.services.ingestion_import_service import (  # noqa: PLC0415
            _resolve_or_create_sentinel_supplier,
        )

        supplier_id = await _resolve_or_create_sentinel_supplier(
            session, tenant.tenant_id, {}
        )
        supplier_name = None

    session.add(
        ExpenseEntry(
            tenant_id=tenant.tenant_id,
            amount=body.amount,
            category=code,
            expense_type=infer_expense_type(code, product_id=target.id),
            transaction_date=body.transaction_date,
            description=body.description or "Compra de mercadería",
            payment_method=body.payment_method,
            product_id=target.id,
            supplier_id=supplier_id,
            supplier_name=supplier_name,
            custom_fields=_apply_category_label({}, code, category_label),
            provenance="REAL",
        )
    )
    record.status = UNCLASSIFIED_STATUS_IMPORTED
    record.resolved_at = datetime.now(UTC)
    await stock_service.increment_stock(
        target.id,
        tenant.tenant_id,
        body.quantity,
        unit_cost,
        source_event_id=f"others:{record_id}",
        db=session,
        supplier_id=supplier_id,
        update_product_cost=True,
        occurred_at=body.transaction_date,
        defer_event=True,
    )
    recompute_requires_completion(target)
    return MessageResponse(message="Compra registrada y vinculada al producto.")


@router.post(
    "/bulk-import",
    response_model=BulkImportResponse,
    summary="Importar en lote los registros de Otros sugeridos como venta/gasto",
)
async def bulk_import_records(
    body: BulkImportRequest,
    tenant: Tenant = Depends(get_current_tenant),
    # F3-T3 review: auth ANTES que el guard 423 (ver nota en resolve_purchase).
    _: User = Depends(require_role("OWNER", "ADMIN")),
    _maintenance_guard: None = Depends(ensure_tenant_not_under_maintenance),
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
