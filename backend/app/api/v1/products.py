"""Product catalog endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_tenant, require_role
from app.application.services.score_trigger_service import trigger_score_recalculation
from app.persistence.db.session import get_db_session
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.persistence.repositories.product_repository import ProductRepository
from app.schemas.common import MessageResponse
from app.schemas.product import CreateProductRequest, ProductResponse, UpdateProductRequest

router = APIRouter()


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
    _: User = Depends(require_role("OWNER", "ADMIN")),
    session: AsyncSession = Depends(get_db_session),
) -> Product:
    repo = ProductRepository(session)
    product = await repo.get_by_id(product_id, tenant.tenant_id)
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found.")
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(product, field, value)
    saved = await repo.save(product)
    trigger_score_recalculation.delay(str(tenant.tenant_id), "product_updated")
    return saved


@router.delete("/{product_id}", response_model=MessageResponse, summary="Soft-delete a product")
async def delete_product(
    product_id: UUID,
    tenant: Tenant = Depends(get_current_tenant),
    _: User = Depends(require_role("OWNER", "ADMIN")),
    session: AsyncSession = Depends(get_db_session),
) -> MessageResponse:
    repo = ProductRepository(session)
    product = await repo.get_by_id(product_id, tenant.tenant_id)
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found.")
    product.is_active = False
    await repo.save(product)
    trigger_score_recalculation.delay(str(tenant.tenant_id), "product_deleted")
    return MessageResponse(message="Product deactivated.")
