"""Product catalog endpoints."""

from datetime import UTC, datetime
from decimal import ROUND_HALF_UP
from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1._stock_manual import (
    aplicar_ajuste_manual_http,
    exigir_costo_con_stock,
    exigir_fecha_con_stock,
    momento_del_costo_del_alta,
    registrar_apertura_http,
)
from app.api.v1.deps import (
    ensure_tenant_not_under_maintenance,
    get_current_tenant,
    require_active_subscription,
    require_modify_access,
    require_pos_permission,
    require_role,
)
from app.application.services import (
    apertura_service,
    maintenance_lock_service,
    stock_inicial_lote_service,
    tenant_categories_service,
)
from app.application.services.costo_promedio_service import costos_a_lote
from app.application.services.idempotency import claim_idempotency_key
from app.application.services.pos_terminal_service import (
    exigir_terminal_si_es_cajero,
    resolver_terminal,
)
from app.application.services.product_identity import (
    ProductIdentityConflictError,
    product_identity_guard,
)
from app.application.services.score_trigger_service import (
    trigger_score_recalculation,
    trigger_score_recalculation_after_commit,
)
from app.config.avg_cost_display_rollout import avg_cost_display_enabled_for
from app.domain.business_time import now_ar_naive, today_ar
from app.domain.costo_promedio import Estado as EstadoPromedio
from app.domain.costo_promedio_de_venta import CENTAVO
from app.domain.costo_vigente import (
    conservar_claves_del_costo,
    custom_fields_tras_editar_costo,
)
from app.domain.pos_permissions import PosPermission
from app.domain.pos_terminal import OFFLINE_HEADER, TERMINAL_HEADER, es_offline
from app.domain.product import DEFAULT_LOW_STOCK_THRESHOLD_UNITS
from app.domain.product_categories import (
    normalize_product_category,
    product_category_catalog,
)
from app.domain.product_category_inference import infer_category
from app.domain.product_completion import recompute_requires_completion
from app.domain.scan_code import TipoDeCodigo, clasificar, variantes_gtin
from app.domain.text_norm import normalize_barcode, normalize_sku
from app.domain.verticals import Vertical, parse_vertical, try_parse_vertical
from app.persistence.db.session import get_db_session
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.business import BusinessProfile
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.models.user import User
from app.persistence.repositories.product_repository import ProductRepository
from app.schemas.common import MessageResponse
from app.schemas.pos import PosProductResponse, a_pos_product
from app.schemas.product import (
    CategorySuggestionResponse,
    CoincidenciaDeFila,
    ConfirmarAperturaRequest,
    ConfirmarAperturaResponse,
    CreateProductRequest,
    EleccionDeFila,
    FilaPreview,
    FilaStockInicial,
    LearnBarcodeRequest,
    LoteStockInicialPreviewRequest,
    LoteStockInicialPreviewResponse,
    LoteStockInicialRequest,
    LoteStockInicialResponse,
    ProductResponse,
    RegistrarAperturaRequest,
    RegistrarAperturaResponse,
    ResultadoDeFila,
    ScanLookupResponse,
    UpdateProductRequest,
)

router = APIRouter()

DEACTIVATION_REASON_MANUAL = "MANUAL_ADMIN_VOID"


async def _tenant_vertical(session: AsyncSession, tenant_id: UUID) -> Vertical:
    """Vertical del tenant, tipado. Sin fallback: sin perfil no hay catálogo.

    Un tenant logueado sin `BusinessProfile` es un estado roto real (signups
    viejos de Google); servirle el catálogo de kiosco era peor que decírselo.
    """
    result = await session.execute(
        select(BusinessProfile.vertical_code).where(BusinessProfile.tenant_id == tenant_id)
    )
    code = result.scalar_one_or_none()
    if code is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="business_profile_not_found"
        )
    return parse_vertical(code)


async def _con_sugerencias(
    session: AsyncSession, tenant_id: UUID, products: list[Product]
) -> list[ProductResponse]:
    """Serializa productos y les agrega ``category_suggestion`` (B·3).

    La categoría inferida del nombre es SÓLO una sugerencia: no se escribe en
    ``products.category`` (un catálogo posterior, que completa sólo vacíos, no
    podría corregirla). Se calcula acá, al leer, y únicamente para productos sin
    categoría con confianza ``high`` — la misma vara que aplica el catálogo. El
    rubro se lee UNA vez por request; un tenant sin perfil o con un rubro no
    reconocido se queda sin sugerencias en vez de romper el listado.
    """
    result = await session.execute(
        select(BusinessProfile.vertical_code).where(BusinessProfile.tenant_id == tenant_id)
    )
    vertical = try_parse_vertical(result.scalar_one_or_none())
    labels = (
        {c["code"]: c["label"] for c in product_category_catalog(vertical)}
        if vertical is not None
        else {}
    )
    promedios = (
        await costos_a_lote(session, tenant_id, [p.id for p in products], today_ar())
        if avg_cost_display_enabled_for(tenant_id)
        else {}
    )
    con_apertura = await apertura_service.productos_con_apertura_viva(
        session, tenant_id, [p.id for p in products]
    )
    out: list[ProductResponse] = []
    for product in products:
        response = ProductResponse.model_validate(product)
        response.needs_initial_stock = bool(product.is_active) and product.id not in con_apertura
        registro = promedios.get(product.id)
        if registro is not None:
            # Sin registro, los tres quedan en null: un producto todavía sin
            # historial (p. ej. antes del backfill) no es «sin compras».
            response.avg_cost_estado = cast(EstadoPromedio, registro.estado)
            response.avg_cost_motivo = registro.motivo
            if registro.estado != "sin_medicion" and registro.costo_promedio is not None:
                response.avg_cost_ars = registro.costo_promedio.quantize(
                    CENTAVO, rounding=ROUND_HALF_UP
                )
        if vertical is not None and not product.category:
            suggestion = infer_category(vertical, product.name)
            if suggestion.confidence == "high" and suggestion.code in labels:
                response.category_suggestion = CategorySuggestionResponse(
                    code=suggestion.code, label=labels[suggestion.code]
                )
        out.append(response)
    return out


async def _resolve_category(
    session: AsyncSession, tenant_id: UUID, raw: str, vertical: Vertical
) -> tuple[str, str | None]:
    """Categoría libre → (code, label). Prioriza las custom del tenant; si no, el
    catálogo del vertical (label solo poblado cuando cae a OTHER)."""
    custom = await tenant_categories_service.resolve_custom_product_category(
        session, tenant_id, raw
    )
    if custom is not None:
        return custom["code"], None
    return normalize_product_category(raw, vertical)


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
    extra: dict[str, object] | None = None,
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
                **(extra or {}),
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
) -> list[ProductResponse]:
    repo = ProductRepository(session)
    products = await repo.list_by_tenant(
        tenant.tenant_id, is_active=is_active, limit=limit, offset=offset
    )
    return await _con_sugerencias(session, tenant.tenant_id, list(products))


@router.get(
    "/count",
    summary="Total de productos del tenant (para paginación) — DEBE ir antes de /{product_id}",
)
async def count_products(
    is_active: bool | None = Query(default=None),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, int]:
    """Corrección C4 (revisión externa 2026-08-19): `getAllProducts` (frontend)
    acumula hasta 5000 productos (25 páginas de 200) sin forma de saber si ese
    límite se alcanzó de verdad o si eso era todo el catálogo."""
    repo = ProductRepository(session)
    total = await repo.count_by_tenant(tenant.tenant_id, is_active=is_active)
    return {"total": total}


@router.get(
    "/categories",
    summary="Catálogo de categorías de producto (vertical + custom del tenant)",
)
async def list_product_categories(
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, str]]:
    vertical = await _tenant_vertical(session, tenant.tenant_id)
    catalog = product_category_catalog(vertical)
    custom = await tenant_categories_service.list_product_categories(session, tenant.tenant_id)
    # Las custom se insertan antes de "OTHER" para que "Otros" quede al final.
    other = [c for c in catalog if c["code"] == "OTHER"]
    base = [c for c in catalog if c["code"] != "OTHER"]
    return [*base, *custom, *other]


class CreateProductCategoryRequest(BaseModel):
    label: str = Field(min_length=1, max_length=100)


@router.post(
    "/custom-categories",
    dependencies=[Depends(require_active_subscription)],
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
    vertical = await _tenant_vertical(session, tenant.tenant_id)
    code, _label = normalize_product_category(body.label, vertical)
    if code != "OTHER":
        match = next(
            (c for c in product_category_catalog(vertical) if c["code"] == code), None
        )
        if match is not None:
            return match
    created = await tenant_categories_service.add_product_category(
        session, tenant.tenant_id, body.label
    )
    if created is None:
        raise HTTPException(status_code=400, detail="No se pudo crear la categoría.")
    return created


async def _tiene_historia(session: AsyncSession, product: Product) -> bool:
    """¿Hay algo registrado que dependa de la unidad actual del producto?"""
    if (product.stock_units or 0) != 0:
        return True
    venta = await session.execute(
        select(SaleEntry.id)
        .where(SaleEntry.tenant_id == product.tenant_id, SaleEntry.product_id == product.id)
        .limit(1)
    )
    if venta.first() is not None:
        return True
    movimiento = await session.execute(
        select(InventoryMovement.id)
        .where(
            InventoryMovement.tenant_id == product.tenant_id,
            InventoryMovement.product_id == product.id,
        )
        .limit(1)
    )
    return movimiento.first() is not None


@router.post(
    "",
    dependencies=[Depends(require_active_subscription)],
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
    user: User = Depends(require_role("OWNER", "ADMIN")),
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
    saved = await _crear_producto(session, tenant.tenant_id, user.user_id, body)
    trigger_score_recalculation.delay(str(tenant.tenant_id), "product_created")
    return saved


async def _crear_producto(
    session: AsyncSession, tenant_id: UUID, user_id: UUID, body: CreateProductRequest
) -> Product:
    """El alta de ``POST /products``, con su stock inicial. La comparte el lote de
    stock inicial (B·5c) para que una fila «crear» no difiera de un alta."""
    repo = ProductRepository(session)
    data = body.model_dump()
    # M·P2c: el stock inicial no se fija en la fila: entra como movimiento de
    # apertura, igual que un catálogo importado en «ya la tenía».
    stock_inicial = data.pop("stock_units")
    costo_envio = data.pop("costo_envio")
    exigir_costo_con_stock(stock_inicial > 0, data.get("unit_cost_ars"))
    exigir_fecha_con_stock(stock_inicial > 0, data.get("acquired_at"))
    data["stock_units"] = 0
    # Un producto por peso o volumen sin umbral propio: el default (5) contado
    # en unidades base serían 5 gramos, que nunca alerta. Se lo lleva a 5 de la
    # unidad de venta (5 kg) y queda explícito, para que todos los lectores
    # —que comparan en unidades base— lo vean igual.
    if data.get("low_stock_threshold_units") is None and data["base_units_per_sale_unit"] > 1:
        data["low_stock_threshold_units"] = (
            DEFAULT_LOW_STOCK_THRESHOLD_UNITS * data["base_units_per_sale_unit"]
        )
    barcode_match, sku_match = await _find_identity_matches(
        session, tenant_id, barcode=data.get("barcode"), sku=data.get("sku")
    )
    conflict = _identity_conflict_response(
        barcode_match, sku_match, barcode=data.get("barcode"), sku=data.get("sku")
    )
    if conflict is not None:
        raise conflict
    # FASE E: normalizar categoría libre al catálogo (custom del tenant primero).
    if data.get("category"):
        vertical = await _tenant_vertical(session, tenant_id)
        code, label = await _resolve_category(
            session, tenant_id, data["category"], vertical
        )
        data["category"] = code
        if label:
            data["custom_fields"] = {
                **(data.get("custom_fields") or {}),
                "category_label": label,
            }
    # M·1c: las claves del costo las escribe sólo el servidor; un alta con costo
    # lo deja como edición manual con su hora (decisión 2).
    data["custom_fields"] = (
        custom_fields_tras_editar_costo(
            conservar_claves_del_costo(None, data.get("custom_fields")),
            None,
            data.get("unit_cost_ars"),
            momento_del_costo_del_alta(stock_inicial, data.get("acquired_at")),
        )
        or conservar_claves_del_costo(None, data.get("custom_fields"))
    )
    product = Product(tenant_id=tenant_id, **data)
    # F5-A: el pre-check de arriba tiene ventana TOCTOU. El guard abre el savepoint,
    # emite el INSERT adentro y traduce la violación del índice único al mismo 409
    # —sin él la request perdedora daría 500 y dejaría la transacción abortada—.
    try:
        async with product_identity_guard(
            session,
            tenant_id=tenant_id,
            barcode=data.get("barcode"),
            sku=data.get("sku"),
        ):
            saved = await repo.save(product)
    except ProductIdentityConflictError as conflict:
        raise _identity_conflict_from_db(
            conflict, barcode=data.get("barcode"), sku=data.get("sku")
        ) from conflict
    if stock_inicial > 0:
        await registrar_apertura_http(
            session,
            tenant_id,
            saved.id,
            stock_inicial,
            costo=data["unit_cost_ars"],
            envio=costo_envio,
            fecha=data["acquired_at"],
            confirmado_por=user_id,
        )
    return saved


@router.get(
    "/lookup",
    response_model=ScanLookupResponse,
    summary="Resolver un código escaneado a UN producto — DEBE ir antes de /{product_id}",
)
async def lookup_product_by_scan(
    code: str = Query(min_length=1, max_length=64),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> ScanLookupResponse:
    """El escaneo de la caja. Clasifica el código y NUNCA elige entre candidatos.

    - 1 producto → 200.
    - 2 o más → 409 ``SCAN_AMBIGUOUS`` con todos: cobrar el producto equivocado
      es peor que pedirle al cajero que elija.
    - 0 y el código se puede aprender → 404 ``SCAN_NOT_FOUND`` con
      ``learnable: true``, para que la caja ofrezca vincularlo.
    - 0 y es de balanza → 422 ``SCAN_SCALE_NOT_SUPPORTED``: leer el peso
      embebido exige un parser que v1 no tiene.
    """
    codigo = clasificar(code)
    candidatos = await ProductRepository(session).find_by_scan(tenant.tenant_id, codigo)
    if len(candidatos) > 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "SCAN_AMBIGUOUS",
                "code_type": codigo.tipo.value,
                # `product` es la vista de CAJA (sin costos, B5): el cajero no
                # puede leer `/products/{id}`, así que sin ella, después de
                # elegir, la caja no tendría el precio para mostrar.
                "candidates": [
                    {
                        "product_id": str(p.id),
                        "name": p.name,
                        "matched_by": columna,
                        "product": a_pos_product(p, tenant.tenant_id).model_dump(
                            mode="json"
                        ),
                    }
                    for p, columna in candidatos
                ],
                "message": "Ese código corresponde a más de un producto. Elegí cuál es.",
            },
        )
    if not candidatos:
        if codigo.tipo is TipoDeCodigo.SCALE:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "code": "SCAN_SCALE_NOT_SUPPORTED",
                    "code_type": codigo.tipo.value,
                    "message": "Los códigos de balanza todavía no se pueden leer.",
                },
            )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "SCAN_NOT_FOUND",
                "code_type": codigo.tipo.value,
                "learnable": codigo.aprendible,
                "message": "No hay ningún producto con ese código.",
            },
        )
    producto, columna = candidatos[0]
    return ScanLookupResponse(
        code_type=codigo.tipo.value,
        matched_by=columna,
        product=a_pos_product(producto, tenant.tenant_id),
    )


@router.get("/{product_id}", response_model=ProductResponse, summary="Get product by ID")
async def get_product(
    product_id: UUID,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> ProductResponse:
    repo = ProductRepository(session)
    product = await repo.get_by_id(product_id, tenant.tenant_id)
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found.")
    return (await _con_sugerencias(session, tenant.tenant_id, [product]))[0]


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
    # M·P2c: el stock no se asigna en absoluto: la diferencia entra al ledger
    # como ajuste manual, después de guardar el resto.
    nuevo_stock = updates.pop("stock_units", None)
    stock_esperado = updates.pop("stock_units_esperado", None)
    costo_envio = updates.pop("costo_envio", None)
    costo_ajuste = updates.get("unit_cost_ars", product.unit_cost_ars)
    if nuevo_stock is not None:
        exigir_costo_con_stock(nuevo_stock > product.stock_units, costo_ajuste)
    # Cambiar la unidad de venta reinterpreta el stock y el historial: 5000
    # "unidades" pasarían a ser 5 kg. Sólo se permite en un producto sin
    # historia; con historia, cambiarla mentiría sobre lo ya vendido y comprado.
    cambia_unidad = (
        updates.get("sale_unit") not in (None, product.sale_unit)
        or updates.get("base_units_per_sale_unit")
        not in (None, product.base_units_per_sale_unit)
    )
    if cambia_unidad and await _tiene_historia(session, product):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "SALE_UNIT_LOCKED",
                "message": (
                    "No se puede cambiar la unidad de venta de un producto con stock, "
                    "ventas o movimientos: cambiaría el significado de lo ya registrado."
                ),
            },
        )
    # Pasar a kilo o litro sin umbral propio: el default de 5 contado en
    # unidades base serían 5 g y nunca alertaría (la misma regla que el alta).
    nuevo_factor = updates.get("base_units_per_sale_unit") or product.base_units_per_sale_unit
    if (
        cambia_unidad
        and nuevo_factor > 1
        and updates.get("low_stock_threshold_units", product.low_stock_threshold_units) is None
    ):
        updates["low_stock_threshold_units"] = DEFAULT_LOW_STOCK_THRESHOLD_UNITS * nuevo_factor
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
        vertical = await _tenant_vertical(session, tenant.tenant_id)
        code, label = await _resolve_category(
            session, tenant.tenant_id, updates["category"], vertical
        )
        updates["category"] = code
        if label:
            updates["custom_fields"] = {
                **(updates.get("custom_fields") or product.custom_fields or {}),
                "category_label": label,
            }
    # M·1c: un PATCH que manda `custom_fields` completo no borra la procedencia del
    # costo, y una edición del costo queda guardada con su hora (decisión 2: vale
    # hasta que llegue una compra con fecha de negocio posterior).
    if "custom_fields" in updates:
        updates["custom_fields"] = conservar_claves_del_costo(
            product.custom_fields, updates["custom_fields"]
        )
    if "unit_cost_ars" in updates:
        _cf_costo = custom_fields_tras_editar_costo(
            updates.get("custom_fields", product.custom_fields),
            product.unit_cost_ars,
            updates["unit_cost_ars"],
            now_ar_naive(),
        )
        if _cf_costo is not None:
            updates["custom_fields"] = _cf_costo
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
    if nuevo_stock is not None:
        await aplicar_ajuste_manual_http(
            session,
            tenant.tenant_id,
            saved.id,
            nuevo_stock,
            esperado=stock_esperado,
            costo=costo_ajuste,
            envio=costo_envio,
            actor=f"usuario {user.user_id}",
        )
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


@router.post(
    "/{product_id}/opening-confirmation",
    response_model=ConfirmarAperturaResponse,
    summary="Confirmar o corregir la fecha del stock inicial de un producto (M·2d)",
)
async def confirm_product_opening(
    product_id: UUID,
    body: ConfirmarAperturaRequest,
    tenant: Tenant = Depends(get_current_tenant),
    # Mueve una fecha del ledger: el mismo permiso que editar el producto.
    user: User = Depends(require_modify_access),
    _maintenance_guard: None = Depends(ensure_tenant_not_under_maintenance),
    session: AsyncSession = Depends(get_db_session),
) -> ConfirmarAperturaResponse:
    """Fija desde cuándo el negocio tiene el stock inicial de este producto.

    - 404: el producto no existe o no tiene stock inicial.
    - 409 ``OPENING_AMBIGUOUS``: tiene más de una carga de stock inicial viva
      (de archivos distintos); se confirma desde cada archivo.

    Las ventas ya costeadas con el stock inicial y anteriores a la fecha nueva no
    cambian de costo: se devuelven para revisarlas.
    """
    try:
        r = await apertura_service.confirmar_apertura_de_producto(
            session, tenant.tenant_id, product_id, body.fecha, user.user_id
        )
    except apertura_service.SinAperturaError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except apertura_service.AperturaAmbiguaError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "OPENING_AMBIGUOUS", "message": str(exc), "cantidad": exc.cantidad},
        ) from exc
    trigger_score_recalculation_after_commit(
        session, str(tenant.tenant_id), "opening_confirmed"
    )
    return ConfirmarAperturaResponse(
        aperturas=r.aperturas,
        productos=r.productos,
        ventas_costeadas_antes=r.ventas_costeadas_antes,
        venta_ids=r.venta_ids,
    )


async def _ventas_sin_costo_desde(
    session: AsyncSession, tenant_id: UUID, product_id: UUID, fecha: datetime
) -> int:
    """Ventas vivas ``sin_costo`` del producto desde la fecha: las que el listener
    costea con esta apertura al confirmar (salvo las de unidad dudosa)."""
    dia = fecha.date()
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(SaleEntry)
                .where(
                    SaleEntry.tenant_id == tenant_id,
                    SaleEntry.product_id == product_id,
                    SaleEntry.voided_at.is_(None),
                    SaleEntry.cost_origin == "sin_costo",
                    func.date(SaleEntry.transaction_date) >= dia,
                )
            )
        ).scalar_one()
    )


@router.post(
    "/{product_id}/opening",
    dependencies=[Depends(require_active_subscription)],
    response_model=RegistrarAperturaResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Cargar el stock inicial de un producto que ya existe (B·5c)",
)
async def register_product_opening(
    product_id: UUID,
    body: RegistrarAperturaRequest,
    tenant: Tenant = Depends(get_current_tenant),
    # Escribe stock y costo: el mismo permiso que editar el producto.
    user: User = Depends(require_modify_access),
    _maintenance_guard: None = Depends(ensure_tenant_not_under_maintenance),
    session: AsyncSession = Depends(get_db_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> RegistrarAperturaResponse:
    """Stock inicial = mercadería que compraron el dueño o los socios, al COSTO.

    - 404: el producto no existe o es de otro negocio.
    - 409 ``PRODUCT_INACTIVE``: está dado de baja.
    - 409 ``OPENING_EXISTS``: ya tiene stock inicial vivo (manual o de un
      archivo); nunca se suma otro en silencio. Trae lo que hay y cómo
      corregirlo con lo que existe hoy.

    Las ventas ``sin_costo`` del producto desde la fecha quedan costeadas al
    confirmar; un costo ya congelado no se revalúa.
    """
    if idempotency_key is not None and not await claim_idempotency_key(
        session, tenant.tenant_id, idempotency_key, "IDEMPOTENT_POST_OPENING"
    ):
        raise HTTPException(status_code=409, detail={"code": "DUPLICATE_IDEMPOTENT"})
    movement = await registrar_apertura_http(
        session,
        tenant.tenant_id,
        product_id,
        body.stock_units,
        costo=body.unit_cost_ars,
        envio=body.costo_envio,
        fecha=body.fecha,
        confirmado_por=user.user_id,
        exigir_sin_apertura=True,
    )
    pendientes = await _ventas_sin_costo_desde(
        session, tenant.tenant_id, product_id, body.fecha
    )
    session.add(
        DecisionAuditLog(
            tenant_id=tenant.tenant_id,
            decision_type="OPENING_STOCK_REGISTERED",
            decision_data={
                "alcance": "producto",
                "product_id": str(product_id),
                "movement_id": str(movement.id),
                "stock_units": body.stock_units,
                "unit_cost_ars": str(body.unit_cost_ars),
                "fecha": body.fecha.date().isoformat(),
                "ventas_sin_costo_desde_la_fecha": pendientes,
            },
            triggered_by="ui:opening_registration",
            actor_user_id=user.user_id,
            created_at=datetime.now(UTC),
        )
    )
    await session.flush()
    trigger_score_recalculation_after_commit(
        session, str(tenant.tenant_id), "opening_registered"
    )
    return RegistrarAperturaResponse(
        movement_id=movement.id,
        product_id=product_id,
        stock_units=body.stock_units,
        ventas_sin_costo_desde_la_fecha=pendientes,
    )


def _preview_de(analisis: list[stock_inicial_lote_service.FilaAnalizada]) -> list[FilaPreview]:
    return [
        FilaPreview(
            fila=a.fila,
            errores=a.errores,
            coincidencias=[
                CoincidenciaDeFila(
                    product_id=c.product_id,
                    nombre=c.nombre,
                    por=c.por,
                    necesita_stock_inicial=c.necesita_stock_inicial,
                    sale_unit=c.sale_unit,
                    base_units_per_sale_unit=c.base_units_per_sale_unit,
                )
                for c in a.coincidencias
            ],
        )
        for a in analisis
    ]


@router.post(
    "/batch/preview",
    response_model=LoteStockInicialPreviewResponse,
    summary="Paso 1 del lote de stock inicial: coincidencias y token (B·5c)",
)
async def preview_initial_stock_batch(
    body: LoteStockInicialPreviewRequest,
    tenant: Tenant = Depends(get_current_tenant),
    _user: User = Depends(require_modify_access),
    session: AsyncSession = Depends(get_db_session),
) -> LoteStockInicialPreviewResponse:
    """No escribe. Por fila: errores y productos activos que coinciden por SKU,
    código de barras o nombre normalizado, y si necesitan stock inicial."""
    analisis = await stock_inicial_lote_service.analizar(session, tenant.tenant_id, body.filas)
    return LoteStockInicialPreviewResponse(
        filas=_preview_de(analisis),
        preview_token=stock_inicial_lote_service.token(body.filas, analisis),
    )


def _error_de_lote(code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={"code": code, "message": message, **extra},
    )


def _productos_en_alta() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "PRODUCTS_BEING_CREATED",
            "message": "Se están creando o cargando productos ahora mismo (otra carga o "
            "un archivo). Probá de nuevo en un momento.",
        },
    )


def _es_lock_no_disponible(exc: DBAPIError) -> bool:
    """``55P03`` (lock_not_available): el ``NOWAIT`` o el ``lock_timeout`` del lote."""
    for err in (exc.orig, getattr(exc.orig, "__cause__", None)):
        if getattr(err, "sqlstate", None) == "55P03" or getattr(err, "pgcode", None) == "55P03":
            return True
    return False


def _campos_no_aplicados(f: FilaStockInicial, p: Product) -> list[str]:
    """Lo que la fila trae distinto del producto existente: ``apertura_en`` carga
    sólo el stock inicial (cantidad, costo, envío) y no edita el producto. Se
    devuelve para que la pantalla lo diga, nunca se descarta en silencio."""
    out: list[str] = []
    if f.sku and normalize_sku(f.sku) != p.sku_normalized:
        out.append("sku")
    if f.barcode and normalize_barcode(f.barcode) != p.barcode_normalized:
        out.append("barcode")
    if f.sale_price_ars.quantize(CENTAVO) != (p.sale_price_ars or 0):
        out.append("sale_price_ars")
    if f.categoria and f.categoria.strip().lower() != (p.category or "").strip().lower():
        out.append("categoria")
    if f.nombre.strip() != p.name.strip():
        out.append("nombre")
    return out


async def _ejecutar_lote(
    session: AsyncSession,
    tid: UUID,
    user_id: UUID,
    body: LoteStockInicialRequest,
    plan: list[tuple[int, str, UUID | None]],
    destinos: list[UUID],
) -> list[ResultadoDeFila]:
    """Escribe el lote. Con el exclusivo de alta tomado, el lote no espera locks
    de fila: un import en curso que ya tocó un destino (y todavía no pidió el
    lock de alta) daría un deadlock. ``NOWAIT`` sobre los destinos y un
    ``lock_timeout`` corto para el resto → 409, nunca un import abortado."""
    if session.get_bind().dialect.name == "postgresql":
        await session.execute(text("SET LOCAL lock_timeout = '5s'"))
        if destinos:
            await session.execute(
                select(Product.id)
                .where(Product.tenant_id == tid, Product.id.in_(sorted(destinos)))
                .order_by(Product.id)
                .with_for_update(nowait=True)
            )
    resultados: list[ResultadoDeFila] = []
    for i, accion, destino_id in plan:
        f = body.filas[i]
        if accion == "crear":
            producto = await _crear_producto(
                session,
                tid,
                user_id,
                CreateProductRequest(
                    name=f.nombre,
                    sku=f.sku,
                    barcode=f.barcode,
                    category=f.categoria,
                    sale_price_ars=f.sale_price_ars,
                    unit_cost_ars=f.unit_cost_ars,
                    stock_units=f.stock_units,
                    costo_envio=f.costo_envio,
                    acquired_at=body.fecha,
                ),
            )
            apertura = await apertura_service.apertura_viva_de(session, tid, producto.id)
            assert apertura is not None  # la acaba de crear el alta
            resultados.append(
                ResultadoDeFila(
                    fila=i, accion="crear", product_id=producto.id, movement_id=apertura.id
                )
            )
        else:
            assert destino_id is not None
            destino = await session.get(Product, destino_id)
            assert destino is not None  # bloqueado arriba
            no_aplicados = _campos_no_aplicados(f, destino)
            # La fila dice kg / L en un producto por peso o volumen: el ledger
            # cuenta unidades base (el mismo criterio que el alta y el PATCH).
            mov = await registrar_apertura_http(
                session,
                tid,
                destino_id,
                f.stock_units * (destino.base_units_per_sale_unit or 1),
                costo=f.unit_cost_ars,
                envio=f.costo_envio,
                fecha=body.fecha,
                confirmado_por=user_id,
                exigir_sin_apertura=True,
            )
            resultados.append(
                ResultadoDeFila(
                    fila=i,
                    accion="apertura_en",
                    product_id=destino_id,
                    movement_id=mov.id,
                    campos_no_aplicados=no_aplicados,
                )
            )
    return resultados


@router.post(
    "/batch",
    dependencies=[Depends(require_active_subscription)],
    response_model=LoteStockInicialResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Paso 2 del lote de stock inicial: crear o abrir, todo o nada (B·5c)",
)
async def confirm_initial_stock_batch(
    body: LoteStockInicialRequest,
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(require_modify_access),
    _maintenance_guard: None = Depends(ensure_tenant_not_under_maintenance),
    session: AsyncSession = Depends(get_db_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> LoteStockInicialResponse:
    """Cada fila crea un producto con su stock inicial, o carga el stock inicial
    de un producto existente que coincide (``apertura_en``). Todo o nada.

    - 409 ``PRODUCTS_BEING_CREATED``: otra alta o un import está creando
      productos ahora; se reintenta en un momento.
    - 409 ``BATCH_CHANGED``: las coincidencias cambiaron desde el preview (trae
      el preview nuevo).
    - 422 ``ROWS_INVALID`` / ``CHOICE_REQUIRED`` / ``INVALID_CHOICE``.
    """
    tid = tenant.tenant_id
    if idempotency_key is not None and not await claim_idempotency_key(
        session, tid, idempotency_key, "IDEMPOTENT_POST_PRODUCT_BATCH"
    ):
        raise HTTPException(status_code=409, detail={"code": "DUPLICATE_IDEMPOTENT"})
    # Mismo orden que cualquier alta: el lock de escritura y después el de alta.
    # Exclusivo y sin esperar: ningún producto nace entre las coincidencias de
    # abajo y los INSERT (el nombre admite homónimos: no hay índice que lo frene).
    await maintenance_lock_service.acquire_write_lock_shared(session, tid)
    if not await maintenance_lock_service.try_product_creation_exclusive(session, tid):
        raise _productos_en_alta()
    analisis = await stock_inicial_lote_service.analizar(session, tid, body.filas)
    if stock_inicial_lote_service.token(body.filas, analisis) != body.preview_token:
        nuevo = LoteStockInicialPreviewResponse(
            filas=_preview_de(analisis),
            preview_token=stock_inicial_lote_service.token(body.filas, analisis),
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "BATCH_CHANGED",
                "message": "Los productos cambiaron desde que revisaste la tabla. "
                "Revisá las coincidencias de nuevo.",
                "preview": nuevo.model_dump(mode="json"),
            },
        )
    con_error = [a for a in analisis if a.errores]
    if con_error:
        raise _error_de_lote(
            "ROWS_INVALID",
            "Hay filas con errores.",
            filas=[{"fila": a.fila, "errores": a.errores} for a in con_error],
        )
    elecciones: dict[int, EleccionDeFila] = {}
    for dada in body.elecciones:
        if dada.fila >= len(body.filas) or dada.fila in elecciones:
            raise _error_de_lote(
                "INVALID_CHOICE", f"Elección inválida para la fila {dada.fila + 1}."
            )
        elecciones[dada.fila] = dada
    plan: list[tuple[int, str, UUID | None]] = []
    # Factor a unidades base por fila (auditoría): el de la coincidencia elegida
    # en ``apertura_en``; un producto nuevo del lote se vende por unidad.
    factor_de: dict[int, int] = {}
    for a in analisis:
        e = elecciones.get(a.fila)
        if e is None:
            if a.coincidencias:
                raise _error_de_lote(
                    "CHOICE_REQUIRED",
                    f"La fila {a.fila + 1} se parece a un producto que ya existe: "
                    "elegí si es ese o uno nuevo.",
                    fila=a.fila,
                )
            plan.append((a.fila, "crear", None))
            continue
        if e.accion == "crear":
            if a.por_identidad:
                raise _error_de_lote(
                    "INVALID_CHOICE",
                    f"La fila {a.fila + 1} tiene el SKU o el código de barras de un "
                    "producto que ya existe: no se puede crear otro.",
                    fila=a.fila,
                )
            plan.append((a.fila, "crear", None))
            continue
        destino = next((c for c in a.coincidencias if c.product_id == e.product_id), None)
        if destino is None or not destino.necesita_stock_inicial:
            raise _error_de_lote(
                "INVALID_CHOICE",
                f"La fila {a.fila + 1}: el producto elegido no coincide o ya tiene "
                "stock inicial.",
                fila=a.fila,
            )
        plan.append((a.fila, "apertura_en", destino.product_id))
        factor_de[a.fila] = destino.base_units_per_sale_unit
    destinos = [d for _, accion, d in plan if accion == "apertura_en" and d is not None]
    if len(set(destinos)) != len(destinos):
        raise _error_de_lote(
            "INVALID_CHOICE",
            "Dos filas eligen el mismo producto: unilas en una sola.",
        )

    try:
        resultados = await _ejecutar_lote(session, tid, user.user_id, body, plan, destinos)
    except DBAPIError as exc:
        if not _es_lock_no_disponible(exc):
            raise
        raise _productos_en_alta() from exc
    creados = sum(1 for r in resultados if r.accion == "crear")
    session.add(
        DecisionAuditLog(
            tenant_id=tid,
            decision_type="OPENING_STOCK_BATCH",
            decision_data={
                "alcance": "lote",
                "fecha": body.fecha.date().isoformat(),
                "filas": [
                    {
                        "fila": r.fila,
                        "accion": r.accion,
                        "product_id": str(r.product_id),
                        "movement_id": str(r.movement_id),
                        # Lo que entró al ledger (unidades base) y lo tipeado.
                        "stock_units": body.filas[r.fila].stock_units
                        * factor_de.get(r.fila, 1),
                        "stock_units_fila": body.filas[r.fila].stock_units,
                        "unit_cost_ars": str(body.filas[r.fila].unit_cost_ars),
                    }
                    for r in resultados
                ],
            },
            triggered_by="ui:opening_batch",
            actor_user_id=user.user_id,
            created_at=datetime.now(UTC),
        )
    )
    await session.flush()
    trigger_score_recalculation_after_commit(session, str(tid), "opening_batch")
    return LoteStockInicialResponse(
        filas=resultados, creados=creados, con_apertura=len(resultados) - creados
    )


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


@router.post(
    "/{product_id}/barcode",
    # Vista de caja: lo llama un cajero y no puede devolverle costos (B5), ni al
    # vincular ni al repetir una vinculación ya hecha.
    response_model=PosProductResponse,
    summary="Vincular un código de barras escaneado a un producto (sólo agrega)",
)
async def learn_product_barcode(
    product_id: UUID,
    body: LearnBarcodeRequest,
    tenant: Tenant = Depends(get_current_tenant),
    # OWNER/ADMIN siempre; un CASHIER, si tiene `learn_barcode`.
    user: User = Depends(require_pos_permission(PosPermission.LEARN_BARCODE)),
    _maintenance_guard: None = Depends(ensure_tenant_not_under_maintenance),
    session: AsyncSession = Depends(get_db_session),
    terminal_secret: str | None = Header(default=None, alias=TERMINAL_HEADER),
    offline: str | None = Header(default=None, alias=OFFLINE_HEADER),
) -> PosProductResponse:
    """El aprendizaje de la caja: "este código es este producto".

    Endpoint propio y no ``PATCH`` por dos razones: el ``PATCH`` lleva
    ``require_modify_access``, y un 428 ``PIN_REQUIRED`` en medio del flush de
    una cola offline es intolerable; y el ``PATCH`` pisa, esto **sólo agrega**.

    Idempotente por contenido: vincular el mismo código al mismo producto otra
    vez devuelve 200 sin tocar nada, así que no hace falta ``Idempotency-Key``.

    Rechazos:
    - 422 ``SCAN_CODE_NOT_LEARNABLE``: sólo se aprende un GTIN válido.
    - 409 ``BARCODE_ALREADY_SET``: el producto ya tiene OTRO código. Cambiarlo
      es una corrección de catálogo, no algo que se decide en la caja.
    - 409 ``BARCODE_TAKEN``: otro producto ya responde a ese código (por barcode
      o por SKU). Para la caja offline es la señal de mandar el aprendizaje a
      revisión; las ventas ya hechas no se tocan.
    """
    # Un cajero sólo escribe desde una caja habilitada (B6).
    terminal = await resolver_terminal(session, tenant.tenant_id, terminal_secret)
    exigir_terminal_si_es_cajero(user, terminal)
    codigo = clasificar(body.barcode)
    if not codigo.aprendible:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "SCAN_CODE_NOT_LEARNABLE",
                "code_type": codigo.tipo.value,
                "message": "Sólo se puede vincular un código de barras de producto (EAN/UPC).",
            },
        )
    # El advisory shared SIEMPRE antes que cualquier FOR UPDATE de fila.
    await maintenance_lock_service.acquire_write_lock_shared(session, tenant.tenant_id)
    producto = (
        await session.execute(
            select(Product)
            .where(
                Product.id == product_id,
                Product.tenant_id == tenant.tenant_id,
                Product.is_active.is_(True),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if producto is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found.")

    variantes = variantes_gtin(codigo.valor)
    if producto.barcode_normalized in variantes:
        return a_pos_product(producto, tenant.tenant_id)
    # Se mira el campo CRUDO, no sólo el normalizado: un código alfanumérico
    # tiene `barcode_normalized = None` y, mirando sólo ese, esto lo pisaría.
    if producto.barcode_normalized is not None or (producto.barcode or "").strip():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "BARCODE_ALREADY_SET",
                "current_barcode": producto.barcode,
                "message": "Ese producto ya tiene otro código de barras.",
            },
        )

    ocupantes = [
        (p, columna)
        for p, columna in await ProductRepository(session).find_by_scan(
            tenant.tenant_id, codigo
        )
        if p.id != producto.id
    ]
    if ocupantes:
        raise _barcode_taken(ocupantes[0][0], ocupantes[0][1])

    before = _product_snapshot(producto)
    try:
        async with product_identity_guard(
            session,
            tenant_id=tenant.tenant_id,
            barcode=codigo.valor,
            sku=producto.sku,
            exclude_id=producto.id,
        ):
            producto.barcode = codigo.valor
            producto.has_user_edits = True
            await session.flush()
    except ProductIdentityConflictError as conflict:
        # La carrera: otra caja vinculó el mismo código entre el pre-check y el
        # flush. El índice único la atrapa; el cliente recibe el mismo 409.
        raise _barcode_taken(conflict.existing, conflict.matched_by) from conflict

    _audit_data_change(
        session,
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        decision_type="PRODUCT_BARCODE_LEARNED",
        before={**before, "barcode": None},
        after={**_product_snapshot(producto), "barcode": producto.barcode},
        # Un código aprendido sin red llega tarde y desde la cola de la caja: la
        # auditoría tiene que poder distinguirlo de uno vinculado en el momento.
        extra={
            "learned_offline": es_offline(offline),
            "terminal_id": str(terminal.id) if terminal is not None else None,
        },
    )
    return a_pos_product(producto, tenant.tenant_id)


def _barcode_taken(ocupante: Product, columna: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "BARCODE_TAKEN",
            "existing_id": str(ocupante.id),
            "existing_name": ocupante.name,
            "field": columna,
            "message": "Ese código ya corresponde a otro producto.",
        },
    )
