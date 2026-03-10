"""Product catalog endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_tenant, require_role
from app.persistence.db.session import get_db_session
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.schemas.common import MessageResponse

router = APIRouter()


class ProductResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    tenant_id: UUID
    name: str
    sku: str | None
    description: str | None
    unit_price: float
    unit_cost: float | None
    category: str | None
    is_active: bool


class CreateProductRequest(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    sku: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    unit_price: float = Field(gt=0)
    unit_cost: float | None = Field(default=None, gt=0)
    category: str | None = Field(default=None, max_length=100)


class UpdateProductRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=300)
    sku: str | None = Field(default=None, max_length=100)
    unit_price: float | None = Field(default=None, gt=0)
    unit_cost: float | None = Field(default=None, gt=0)
    category: str | None = Field(default=None, max_length=100)
    is_active: bool | None = None


@router.get("", response_model=list[ProductResponse], summary="List products")
async def list_products(
    is_active: bool | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> list[Product]:
    q = select(Product).where(Product.tenant_id == tenant.id)
    if is_active is not None:
        q = q.where(Product.is_active == is_active)
    q = q.limit(limit).offset(offset)
    result = await session.execute(q)
    return list(result.scalars().all())


@router.post(
    "",
    response_model=ProductResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a product",
)
async def create_product(
    body: CreateProductRequest,
    tenant: Tenant = Depends(get_current_tenant),
    _: User = Depends(require_role("owner", "admin")),
    session: AsyncSession = Depends(get_db_session),
) -> Product:
    product = Product(tenant_id=tenant.id, **body.model_dump())
    session.add(product)
    await session.flush()
    return product


@router.get("/{product_id}", response_model=ProductResponse, summary="Get product by ID")
async def get_product(
    product_id: UUID,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> Product:
    result = await session.execute(
        select(Product).where(Product.id == product_id, Product.tenant_id == tenant.id)
    )
    product = result.scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found.")
    return product


@router.patch("/{product_id}", response_model=ProductResponse, summary="Update a product")
async def update_product(
    product_id: UUID,
    body: UpdateProductRequest,
    tenant: Tenant = Depends(get_current_tenant),
    _: User = Depends(require_role("owner", "admin")),
    session: AsyncSession = Depends(get_db_session),
) -> Product:
    result = await session.execute(
        select(Product).where(Product.id == product_id, Product.tenant_id == tenant.id)
    )
    product = result.scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found.")
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(product, field, value)
    session.add(product)
    await session.flush()
    return product


@router.delete("/{product_id}", response_model=MessageResponse, summary="Delete a product")
async def delete_product(
    product_id: UUID,
    tenant: Tenant = Depends(get_current_tenant),
    _: User = Depends(require_role("owner", "admin")),
    session: AsyncSession = Depends(get_db_session),
) -> MessageResponse:
    result = await session.execute(
        select(Product).where(Product.id == product_id, Product.tenant_id == tenant.id)
    )
    product = result.scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found.")
    await session.delete(product)
    await session.flush()
    return MessageResponse(message="Product deleted.")
