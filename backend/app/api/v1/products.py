"""Product catalog endpoints."""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_tenant, require_role
from app.application.services.score_trigger_service import trigger_score_recalculation
from app.persistence.db.session import get_db_session
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.persistence.repositories.product_repository import ProductRepository
from app.schemas.common import MessageResponse
from app.schemas.product import CreateProductRequest, ProductResponse, UpdateProductRequest

router = APIRouter()

DEACTIVATION_REASON_MANUAL = "MANUAL_ADMIN_VOID"


def _product_snapshot(product: Product) -> dict[str, object]:
    return {
        "id": str(product.id),
        "name": product.name,
        "sku": product.sku,
        "description": product.description,
        "category": product.category,
        "sale_price_ars": str(product.sale_price_ars),
        "unit_cost_ars": str(product.unit_cost_ars) if product.unit_cost_ars is not None else None,
        "stock_units": product.stock_units,
        "low_stock_threshold_units": product.low_stock_threshold_units,
        "is_active": product.is_active,
        "custom_fields": product.custom_fields,
        "deactivated_at": product.deactivated_at.isoformat() if product.deactivated_at else None,
        "deactivation_reason": product.deactivation_reason,
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
                "record_type": "product",
                "record_id": before["id"],
                "before": before,
                "after": after,
                "source": "ui",
            },
            triggered_by="ui:data_records",
            actor_user_id=user_id,
            context={"endpoint": "products"},
            created_at=datetime.now(UTC),
        )
    )


@router.get("", response_model=list[ProductResponse], summary="List products")
async def list_products(
    is_active: bool | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> list[Product]:
    repo = ProductRepository(session)
    return await repo.list_by_tenant(
        tenant.tenant_id, is_active=is_active, limit=limit, offset=offset
    )


@router.post(
    "",
    response_model=ProductResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a product",
)
async def create_product(
    body: CreateProductRequest,
    tenant: Tenant = Depends(get_current_tenant),
    _: User = Depends(require_role("OWNER", "ADMIN")),
    session: AsyncSession = Depends(get_db_session),
) -> Product:
    repo = ProductRepository(session)
    product = Product(tenant_id=tenant.tenant_id, **body.model_dump())
    saved = await repo.save(product)
    trigger_score_recalculation.delay(str(tenant.tenant_id), "product_created")
    return saved


@router.get("/{product_id}", response_model=ProductResponse, summary="Get product by ID")
async def get_product(
    product_id: UUID,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> Product:
    repo = ProductRepository(session)
    product = await repo.get_by_id(product_id, tenant.tenant_id)
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found.")
    return product


@router.patch("/{product_id}", response_model=ProductResponse, summary="Update a product")
async def update_product(
    product_id: UUID,
    body: UpdateProductRequest,
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(require_role("OWNER", "ADMIN")),
    session: AsyncSession = Depends(get_db_session),
) -> Product:
    repo = ProductRepository(session)
    product = await repo.get_by_id(product_id, tenant.tenant_id)
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found.")
    before = _product_snapshot(product)
    # exclude_unset (no exclude_none): aplica solo los campos enviados por el cliente,
    # permitiendo limpiar opcionales a null (ej. borrar acquired_at). Los campos no
    # enviados quedan intactos.
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(product, field, value)
    # FASE 3 (B2): si el producto estaba marcado para completar y ya tiene precio
    # y costo, se considera completo (cierra el ciclo del auto-creado por import).
    if (
        product.requires_completion
        and product.sale_price_ars
        and product.sale_price_ars > 0
        and product.unit_cost_ars is not None
    ):
        product.requires_completion = False
    saved = await repo.save(product)
    _audit_data_change(
        session,
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        decision_type="DATA_RECORD_UPDATED",
        before=before,
        after=_product_snapshot(saved),
    )
    trigger_score_recalculation.delay(str(tenant.tenant_id), "product_updated")
    return saved


@router.delete("/{product_id}", response_model=MessageResponse, summary="Soft-delete a product")
async def delete_product(
    product_id: UUID,
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(require_role("OWNER", "ADMIN")),
    session: AsyncSession = Depends(get_db_session),
) -> MessageResponse:
    repo = ProductRepository(session)
    product = await repo.get_by_id(product_id, tenant.tenant_id)
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found.")
    before = _product_snapshot(product)
    product.is_active = False
    product.deactivated_at = datetime.now(UTC)
    product.deactivation_reason = DEACTIVATION_REASON_MANUAL
    await repo.save(product)
    _audit_data_change(
        session,
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        decision_type="DATA_RECORD_VOIDED",
        before=before,
        after=_product_snapshot(product),
    )
    trigger_score_recalculation.delay(str(tenant.tenant_id), "product_deleted")
    return MessageResponse(message="Product deactivated.")
