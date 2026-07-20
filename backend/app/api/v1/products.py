"""Product catalog endpoints."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import (
    ensure_tenant_not_under_maintenance,
    get_current_tenant,
    require_modify_access,
    require_role,
)
from app.application.services import tenant_categories_service
from app.application.services.idempotency import claim_idempotency_key
from app.application.services.product_identity import (
    ProductIdentityConflictError,
    product_identity_guard,
)
from app.application.services.score_trigger_service import trigger_score_recalculation
from app.domain.product_categories import (
    normalize_product_category,
    product_category_catalog,
)
from app.domain.product_completion import recompute_requires_completion
from app.domain.text_norm import normalize_barcode, normalize_sku
from app.persistence.db.session import get_db_session
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.business import BusinessProfile
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.persistence.repositories.product_repository import ProductRepository
from app.schemas.common import MessageResponse
from app.schemas.product import CreateProductRequest, ProductResponse, UpdateProductRequest

router = APIRouter()

DEACTIVATION_REASON_MANUAL = "MANUAL_ADMIN_VOID"


async def _tenant_business_type(session: AsyncSession, tenant_id: UUID) -> str | None:
    result = await session.execute(
        select(BusinessProfile.vertical_code).where(BusinessProfile.tenant_id == tenant_id)
    )
    return result.scalar_one_or_none()


async def _resolve_category(
    session: AsyncSession, tenant_id: UUID, raw: str, business_type: str | None
) -> tuple[str, str | None]:
    """Categoría libre → (code, label). Prioriza las custom del tenant; si no, el
    catálogo del vertical (label solo poblado cuando cae a OTHER)."""
    custom = await tenant_categories_service.resolve_custom_product_category(
        session, tenant_id, raw
    )
    if custom is not None:
        return custom["code"], None
    return normalize_product_category(raw, business_type)


async def _find_identity_matches(
    session: AsyncSession,
    tenant_id: UUID,
    *,
    barcode: str | None,
    sku: str | None,
    exclude_id: UUID | None = None,
) -> tuple[Product | None, Product | None]:
    """Devuelve ``(barcode_match, sku_match)``: los productos ACTIVOS del tenant
    que ya usan ese barcode y ese sku (por las columnas normalizadas de T1),
    consultados por SEPARADO — nunca ``OR`` + ``.first()`` (review F2 #6): con un
    ``OR`` un barcode que matchea A y un sku que matchea B devuelven arbitrariamente
    uno, ocultando que es un CONFLICTO entre identidades, no un duplicado simple.

    ``exclude_id`` excluye el propio producto (para el PATCH — no es duplicado de
    sí mismo). Solo dispara con barcode/sku informados; un nombre repetido en
    soledad es un homónimo legítimo, no un duplicado. ``.first()`` (no
    ``scalar_one_or_none``): el índice único es Fase 5, todavía puede haber >1 fila.
    """
    barcode_norm = normalize_barcode(barcode)
    sku_norm = normalize_sku(sku)

    async def _first(condition: Any) -> Product | None:
        stmt = select(Product).where(
            Product.tenant_id == tenant_id,
            Product.is_active.is_(True),
            condition,
        )
        if exclude_id is not None:
            stmt = stmt.where(Product.id != exclude_id)
        return (await session.execute(stmt.limit(1))).scalars().first()

    barcode_match = (
        await _first(Product.barcode_normalized == barcode_norm)
        if barcode_norm is not None
        else None
    )
    sku_match = (
        await _first(Product.sku_normalized == sku_norm) if sku_norm is not None else None
    )
    return barcode_match, sku_match


def _identity_conflict_response(
    barcode_match: Product | None,
    sku_match: Product | None,
    *,
    barcode: str | None,
    sku: str | None,
) -> HTTPException | None:
    """Traduce ``(barcode_match, sku_match)`` a la HTTPException 409 apropiada, o
    ``None`` si no hay colisión.

    - barcode y sku matchean productos DISTINTOS → ``IDENTITY_CONFLICT`` (409) con
      AMBOS candidatos (review F2 #6: no elegir uno arbitrario).
    - solo uno matchea (o ambos al MISMO producto) → ``DUPLICATE_PRODUCT_IDENTITY``.
    """
    if (
        barcode_match is not None
        and sku_match is not None
        and barcode_match.id != sku_match.id
    ):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "IDENTITY_CONFLICT",
                "barcode_product_id": str(barcode_match.id),
                "sku_product_id": str(sku_match.id),
                "message": "El código de barras y el SKU apuntan a productos "
                "distintos. Revisá cuál corresponde.",
            },
        )
    existing = barcode_match or sku_match
    if existing is not None:
        return _duplicate_identity_conflict(existing, barcode=barcode, sku=sku)
    return None


def _identity_conflict_from_db(
    conflict: ProductIdentityConflictError, *, barcode: str | None, sku: str | None
) -> HTTPException:
    """Traduce la violación del índice único al MISMO 409 que el pre-check.

    F5-A: ``_find_identity_matches`` es un SELECT-antes-de-INSERT con ventana TOCTOU
    —dos requests concurrentes pasan las dos—. Cuando el índice de la DB atrapa a la
    perdedora, el cliente tiene que recibir el mismo error que si hubiera perdido el
    pre-check: un 500 por carrera es indistinguible de un bug para quien lo consume.
    """
    if conflict.other is not None:
        by_barcode = (
            conflict.existing if conflict.matched_by == "barcode" else conflict.other
        )
        by_sku = conflict.other if conflict.matched_by == "barcode" else conflict.existing
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "IDENTITY_CONFLICT",
                "barcode_product_id": str(by_barcode.id),
                "sku_product_id": str(by_sku.id),
                "message": "El código de barras y el SKU apuntan a productos "
                "distintos. Revisá cuál corresponde.",
            },
        )
    return _duplicate_identity_conflict(conflict.existing, barcode=barcode, sku=sku)


async def _find_active_product_by_identity(
    session: AsyncSession,
    tenant_id: UUID,
    *,
    barcode: str | None,
    sku: str | None,
) -> Product | None:
    """Compat: primer producto activo que matchee barcode o sku (sin distinguir
    conflicto). Reusado por ``/otros`` en el path de crear-nuevo."""
    barcode_match, sku_match = await _find_identity_matches(
        session, tenant_id, barcode=barcode, sku=sku
    )
    return barcode_match or sku_match


def _duplicate_identity_conflict(
    existing: Product, *, barcode: str | None, sku: str | None
) -> HTTPException:
    """Arma el 409 de identidad duplicada. Prioriza barcode como campo reportado
    cuando ambos matchean (barcode es el identificador más fuerte)."""
    barcode_norm = normalize_barcode(barcode)
    field = (
        "barcode"
        if barcode_norm is not None and existing.barcode_normalized == barcode_norm
        else "sku"
    )
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "DUPLICATE_PRODUCT_IDENTITY",
            "existing_id": str(existing.id),
            "field": field,
            "message": "Ya existe un producto activo con ese "
            + ("código de barras." if field == "barcode" else "SKU."),
        },
    )


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


@router.get(
    "/categories",
    summary="Catálogo de categorías de producto (vertical + custom del tenant)",
)
async def list_product_categories(
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, str]]:
    business_type = await _tenant_business_type(session, tenant.tenant_id)
    catalog = product_category_catalog(business_type)
    custom = await tenant_categories_service.list_product_categories(session, tenant.tenant_id)
    # Las custom se insertan antes de "OTHER" para que "Otros" quede al final.
    other = [c for c in catalog if c["code"] == "OTHER"]
    base = [c for c in catalog if c["code"] != "OTHER"]
    return [*base, *custom, *other]


class CreateProductCategoryRequest(BaseModel):
    label: str = Field(min_length=1, max_length=100)


@router.post(
    "/custom-categories",
    status_code=status.HTTP_201_CREATED,
    summary="Crear una categoría de producto personalizada del tenant",
)
async def create_product_category(
    body: CreateProductCategoryRequest,
    tenant: Tenant = Depends(get_current_tenant),
    _: User = Depends(require_role("OWNER", "ADMIN")),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, str]:
    # Si el label coincide con una categoría canónica del vertical, devolver ESA
    # (no crear una custom que colisione: evita CUSTOM_BEBIDAS vs BEBIDAS y mantiene
    # consistente el código entre carga manual e import/chat).
    business_type = await _tenant_business_type(session, tenant.tenant_id)
    code, _label = normalize_product_category(body.label, business_type)
    if code != "OTHER":
        match = next(
            (c for c in product_category_catalog(business_type) if c["code"] == code), None
        )
        if match is not None:
            return match
    created = await tenant_categories_service.add_product_category(
        session, tenant.tenant_id, body.label
    )
    if created is None:
        raise HTTPException(status_code=400, detail="No se pudo crear la categoría.")
    return created


@router.post(
    "",
    response_model=ProductResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a product",
)
async def create_product(
    body: CreateProductRequest,
    tenant: Tenant = Depends(get_current_tenant),
    # F3-T3 review: la auth (rol) va ANTES que el guard 423 — si no, un VIEWER con
    # el tenant lockeado recibiría 423 en vez de 403 (filtra estado de mantenimiento
    # a un usuario sin permiso). El guard 423 es solo UX fast-fail; la garantía real
    # es el advisory lock que toma ProductRepository.save().
    _: User = Depends(require_role("OWNER", "ADMIN")),
    _maintenance_guard: None = Depends(ensure_tenant_not_under_maintenance),
    session: AsyncSession = Depends(get_db_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Product:
    # Idempotencia opcional: si llega la key, reclamarla ANTES de crear. Comparte
    # transacción con la creación (commit único en get_db_session), por lo que si
    # la creación falla el claim se revierte y un reintento futuro puede entrar.
    if idempotency_key is not None and not await claim_idempotency_key(
        session, tenant.tenant_id, idempotency_key, "IDEMPOTENT_POST_PRODUCT"
    ):
        raise HTTPException(status_code=409, detail={"code": "DUPLICATE_IDEMPOTENT"})
    repo = ProductRepository(session)
    data = body.model_dump()
    barcode_match, sku_match = await _find_identity_matches(
        session, tenant.tenant_id, barcode=data.get("barcode"), sku=data.get("sku")
    )
    conflict = _identity_conflict_response(
        barcode_match, sku_match, barcode=data.get("barcode"), sku=data.get("sku")
    )
    if conflict is not None:
        raise conflict
    # FASE E: normalizar categoría libre al catálogo (custom del tenant primero).
    if data.get("category"):
        business_type = await _tenant_business_type(session, tenant.tenant_id)
        code, label = await _resolve_category(
            session, tenant.tenant_id, data["category"], business_type
        )
        data["category"] = code
        if label:
            data["custom_fields"] = {
                **(data.get("custom_fields") or {}),
                "category_label": label,
            }
    product = Product(tenant_id=tenant.tenant_id, **data)
    # F5-A: el pre-check de arriba tiene ventana TOCTOU. El guard abre el savepoint,
    # emite el INSERT adentro y traduce la violación del índice único al mismo 409
    # —sin él la request perdedora daría 500 y dejaría la transacción abortada—.
    try:
        async with product_identity_guard(
            session,
            tenant_id=tenant.tenant_id,
            barcode=data.get("barcode"),
            sku=data.get("sku"),
        ):
            saved = await repo.save(product)
    except ProductIdentityConflictError as conflict:
        raise _identity_conflict_from_db(
            conflict, barcode=data.get("barcode"), sku=data.get("sku")
        ) from conflict
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


@router.patch(
    "/{product_id}",
    response_model=ProductResponse,
    summary="Update a product",
)
async def update_product(
    product_id: UUID,
    body: UpdateProductRequest,
    tenant: Tenant = Depends(get_current_tenant),
    # F3-T3 review: auth ANTES que el guard 423 (ver nota en create_product).
    user: User = Depends(require_modify_access),
    _maintenance_guard: None = Depends(ensure_tenant_not_under_maintenance),
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
    updates = body.model_dump(exclude_unset=True)
    # Review F2 #2: validar identidad duplicada ANTES de aplicar un cambio de
    # sku/barcode (el PATCH aplicaba setattr directo → dos productos activos con
    # el mismo SKU). Solo se chequea el identificador que efectivamente cambia,
    # excluyendo el propio producto. Mismo criterio de conflicto que POST.
    # F5-A: la identidad EFECTIVA post-PATCH, no solo los campos presentes en el
    # body. El índice de F5-B es parcial sobre ``is_active``, así que un PATCH que
    # solo reactiva (``{"is_active": true}``) puede violarlo con el sku que el
    # producto YA tenía: alguien ocupó esa clave mientras estaba dado de baja.
    # Con las claves enviadas (ambas None) ni el pre-check corre ni el guard sabría
    # a quién buscar, y el IntegrityError se re-propagaría crudo → 500
    # determinístico, sin carrera de por medio.
    effective_sku = updates.get("sku", product.sku)
    effective_barcode = updates.get("barcode", product.barcode)
    effective_active = updates.get("is_active", product.is_active)
    reactivating = not product.is_active and bool(effective_active)
    identity_changed = "sku" in updates or "barcode" in updates
    if identity_changed or reactivating:
        barcode_match, sku_match = await _find_identity_matches(
            session,
            tenant.tenant_id,
            barcode=effective_barcode,
            sku=effective_sku,
            exclude_id=product.id,
        )
        conflict = _identity_conflict_response(
            barcode_match, sku_match, barcode=effective_barcode, sku=effective_sku
        )
        if conflict is not None:
            raise conflict
    # FASE E: normalizar categoría libre al catálogo (custom del tenant primero).
    if updates.get("category"):
        business_type = await _tenant_business_type(session, tenant.tenant_id)
        code, label = await _resolve_category(
            session, tenant.tenant_id, updates["category"], business_type
        )
        updates["category"] = code
        if label:
            updates["custom_fields"] = {
                **(updates.get("custom_fields") or product.custom_fields or {}),
                "category_label": label,
            }
    # F5-A: los ``setattr`` van DENTRO del guard. Mutar antes deja el objeto ``dirty``
    # y el flush incondicional de ``begin_nested()`` emitiría el UPDATE FUERA del
    # savepoint — exactamente el defecto que el guard existe para evitar.
    try:
        async with product_identity_guard(
            session,
            tenant_id=tenant.tenant_id,
            barcode=effective_barcode,
            sku=effective_sku,
            exclude_id=product.id,
        ):
            for field, value in updates.items():
                setattr(product, field, value)
            # Reactivar limpia la metadata de baja. Dejar ``is_active=True`` junto a
            # ``deactivated_at``/``deactivation_reason`` viejos sería otra
            # inconsistencia determinística: la baja ya no existe.
            if reactivating:
                product.deactivated_at = None
                product.deactivation_reason = None
            # FASE 3 (B2): si el producto estaba marcado para completar y ya tiene
            # precio y costo, se considera completo (cierra el ciclo del auto-creado
            # por import).
            recompute_requires_completion(product)
            # Relectura de archivos: Product no tiene `source_upload_id`, así que
            # cualquier edición manual lo marca como editado (la re-importación no
            # debe pisarlo).
            product.has_user_edits = True
            saved = await repo.save(product)
    except ProductIdentityConflictError as conflict:
        raise _identity_conflict_from_db(
            conflict, barcode=effective_barcode, sku=effective_sku
        ) from conflict
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
    user: User = Depends(require_modify_access),
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
