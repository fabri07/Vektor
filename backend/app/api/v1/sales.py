"""Sales entry endpoints."""

import re
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import (
    ensure_tenant_not_under_maintenance,
    get_current_tenant,
    require_modify_access,
    require_role,
)
from app.application.services import maintenance_lock_service, stock_service
from app.application.services.customer_sentinel import (
    resolve_or_create_local_sentinel,
)
from app.application.services.idempotency import claim_idempotency_key
from app.application.services.ingestion_import_service import (
    UNLINKED_PRODUCT_NAME_FIELD,
)
from app.application.services.score_trigger_service import trigger_score_recalculation
from app.domain.product_alias import add_alias
from app.persistence.db.session import get_db_session
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.product import Product
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
    LinkProductQueueRequest,
    LinkProductQueueResponse,
    ManualBatchSaleRequest,
    ManualBatchSaleResponse,
    ProductLinkCandidate,
    ProductLinkQueueGroup,
    ProductLinkQueueResponse,
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


async def _resolve_sale_customer(
    session: AsyncSession,
    tenant_id: UUID,
    customer_id: UUID | None,
    *,
    is_fiado: bool,
) -> UUID:
    """Devuelve el customer_id a persistir aplicando las reglas de cliente.

    - customer_id dado → valida pertenencia al tenant; fiado al sentinela "Local" → 400.
    - sin customer_id + fiado → 400 (no se fía a "Local").
    - sin customer_id + no fiado → sentinela "Local".
    """
    if customer_id is not None:
        customer = await CustomerRepository(session).get_by_id(customer_id, tenant_id)
        if customer is None:
            raise HTTPException(status_code=400, detail="Customer not found for this tenant.")
        if is_fiado and customer.is_sentinel:
            raise HTTPException(status_code=400, detail=_FIADO_REQUIRES_CUSTOMER)
        return customer_id
    if is_fiado:
        raise HTTPException(status_code=400, detail=_FIADO_REQUIRES_CUSTOMER)
    return await resolve_or_create_local_sentinel(session, tenant_id)


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
    # F3 review final: la auth (rol) va ANTES que el guard 423 — mismo orden que
    # products/others/suppliers (ver create_product).
    _: User = Depends(require_role("OWNER", "ADMIN")),
    _maintenance_guard: None = Depends(ensure_tenant_not_under_maintenance),
    session: AsyncSession = Depends(get_db_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> list[SaleEntry]:
    # Idempotencia opcional: un reintento de red no debe duplicar ventas NI doble-descontar
    # stock (ahora que la carga masiva mueve inventario). Mismo patrón que POST /sales.
    if idempotency_key is not None and not await claim_idempotency_key(
        session, tenant.tenant_id, idempotency_key, "IDEMPOTENT_POST_SALE_BULK"
    ):
        raise HTTPException(status_code=409, detail={"code": "DUPLICATE_IDEMPOTENT"})
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
    # Validar stock ANTES de persistir: NO se permite stock negativo. Se valida contra la
    # SUMA de cantidades por producto (varias líneas del mismo producto se acumulan), no
    # por línea aislada. Si algún producto excede su stock → InsufficientStockError →
    # handler global 400 y toda la carga aborta (rollback: no queda ninguna venta). Filas
    # sin product_id (agregado) no tocan stock. Orden estable por id (anti-deadlock).
    qty_by_product: dict[UUID, int] = {}
    for item in body.entries or []:
        if item.product_id is not None:
            qty_by_product[item.product_id] = qty_by_product.get(item.product_id, 0) + item.quantity
    for pid in sorted(qty_by_product, key=str):
        await stock_service.check_stock_available(
            pid, tenant.tenant_id, qty_by_product[pid], session
        )
    saved = await repo.bulk_save(entries)
    # Descontar stock por línea con producto (idempotente). Las entries de agregado sin
    # product_id no-opean. Ya validado arriba: el descuento no puede dejar stock negativo.
    for e in saved:
        await stock_service.decrement_for_sale(e, session)
    trigger_score_recalculation.delay(str(tenant.tenant_id), "sales_bulk_created")
    return saved


@router.post(
    "/manual-batch",
    response_model=ManualBatchSaleResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a multi-product manual sale (one SaleEntry per line, atomic)",
)
async def create_manual_batch_sale(
    body: ManualBatchSaleRequest,
    tenant: Tenant = Depends(get_current_tenant),
    # F3 review final: la auth (rol) va ANTES que el guard 423 — mismo orden que
    # products/others/suppliers (ver create_product).
    user: User = Depends(require_role("OWNER", "ADMIN")),
    _maintenance_guard: None = Depends(ensure_tenant_not_under_maintenance),
    session: AsyncSession = Depends(get_db_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ManualBatchSaleResponse:
    """Venta manual con varias líneas: una SaleEntry por ítem, mismo encabezado.

    Atómica: si una línea falla (producto inexistente, sobreventa), la transacción
    entera revierte (no quedan ventas ni descuentos de stock parciales). Descuenta
    stock de cada producto (la venta manual antes NO lo hacía). `sale_group_id`
    (en `custom_fields`) agrupa y audita las líneas de la operación.

    Concurrencia: bloquea las filas de producto con ``SELECT ... FOR UPDATE`` en
    orden estable (por id) para evitar deadlocks y sobreventa entre requests
    simultáneos. Rechaza productos duplicados en el mismo carrito.
    """
    if idempotency_key is not None and not await claim_idempotency_key(
        session, tenant.tenant_id, idempotency_key, "IDEMPOTENT_POST_SALE_BATCH"
    ):
        raise HTTPException(status_code=409, detail={"code": "DUPLICATE_IDEMPOTENT"})

    # F3 review final: el advisory shared SIEMPRE antes que cualquier FOR UPDATE de fila
    # (acá abajo) — si no, deadlockea AB-BA contra el exclusive del script de dedup. Antes
    # el shared recién se tomaba en decrement_for_sale, después del FOR UPDATE de este
    # handler. Idempotente dentro de la txn: no molesta que stock_service lo pida de nuevo.
    await maintenance_lock_service.acquire_write_lock_shared(session, tenant.tenant_id)

    # Rechazar productos duplicados (evita ambigüedad de stock agregado por línea).
    product_ids = [item.product_id for item in body.items]
    if len(set(product_ids)) != len(product_ids):
        raise HTTPException(
            status_code=400,
            detail="Hay productos repetidos en la venta. Unificá la cantidad en una sola línea.",
        )

    customer_id = await _resolve_sale_customer(
        session,
        tenant.tenant_id,
        body.customer_id,
        is_fiado=body.payment_method == FIADO_PAYMENT_METHOD,
    )

    # Bloqueo de filas en orden estable (por id) — anti-deadlock + anti-sobreventa concurrente.
    ordered_ids = sorted(product_ids, key=str)
    locked = (
        await session.execute(
            select(Product)
            .where(
                Product.tenant_id == tenant.tenant_id,
                Product.id.in_(ordered_ids),
                Product.is_active.is_(True),
            )
            .order_by(Product.id)
            .with_for_update()
        )
    ).scalars().all()
    by_id = {p.id: p for p in locked}
    for item in body.items:
        product = by_id.get(item.product_id)
        if product is None:
            raise HTTPException(
                status_code=400, detail=f"Producto {item.product_id} no encontrado."
            )
        # stock_units es NOT NULL (default 0): validar siempre.
        if product.stock_units < item.quantity:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Stock insuficiente para «{product.name}»: "
                    f"disponible {product.stock_units}, pedís {item.quantity}."
                ),
            )

    repo = SaleRepository(session)
    group_id = uuid.uuid4()
    saved: list[SaleEntry] = []
    total = Decimal("0")
    for item in body.items:
        amount = (item.unit_price * item.quantity).quantize(Decimal("0.01"))
        entry = await repo.save(
            SaleEntry(
                tenant_id=tenant.tenant_id,
                amount=amount,
                quantity=item.quantity,
                transaction_date=body.transaction_date,
                payment_method=body.payment_method,
                product_id=item.product_id,
                customer_id=customer_id,
                notes=body.notes,
                custom_fields={"sale_group_id": str(group_id)},
            )
        )
        # Descuento vía el helper compartido idempotente (source_event_id="sale:{id}"),
        # por línea → habilita reversa exacta en PATCH/DELETE. El bloqueo por stock
        # insuficiente (POS deliberado) ya se validó arriba con SELECT ... FOR UPDATE.
        await stock_service.decrement_for_sale(entry, session)
        # Auditar cada línea con el mismo sale_group_id.
        snap = _sale_snapshot(entry)
        _audit_data_change(
            session,
            tenant_id=tenant.tenant_id,
            user_id=user.user_id,
            decision_type="DATA_RECORD_CREATED",
            before={"id": snap["id"], "sale_group_id": str(group_id)},
            after=snap,
        )
        saved.append(entry)
        total += amount

    trigger_score_recalculation.delay(str(tenant.tenant_id), "sale_batch_created")
    return ManualBatchSaleResponse(
        sale_group_id=group_id,
        sales=[SaleEntryResponse.model_validate(s) for s in saved],
        total=float(total),
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
    customer_id = await _resolve_sale_customer(
        session,
        tenant.tenant_id,
        body.customer_id,
        is_fiado=body.payment_method == FIADO_PAYMENT_METHOD,
    )
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
    # NO se permite stock negativo: validar ANTES de persistir. Si no alcanza, 400 (handler
    # global) y no se crea la SaleEntry ni el movimiento. Sin product_id → no toca stock.
    if body.product_id is not None:
        await stock_service.check_stock_available(
            body.product_id, tenant.tenant_id, body.quantity, session
        )
    saved = await repo.save(entry)
    # Venta EN VIVO con producto → descontar stock (idempotente). Ya validado arriba, así
    # que no puede dejar stock negativo. Sin product_id → no-op. El import histórico NO
    # pasa por acá.
    await stock_service.decrement_for_sale(saved, session)
    trigger_score_recalculation.delay(str(tenant.tenant_id), "sale_entry_created")
    return saved


# ── F-S.0: cola de ventas sin producto vinculado ────────────────────────────────
#
# IMPORTANTE — orden de rutas: estas dos van ANTES de `GET /{sale_id}` (abajo).
# Starlette matchea rutas en el orden en que se registran; si `/{sale_id}`
# fuera primero, `GET /sales/product-link-queue` matchearía ahí y
# "product-link-queue" fallaría la validación de UUID (422), nunca 200.

#: Filas ESCANEADAS por página (no filas que califican). Keyset por `id`, no
#: OFFSET: con miles de ventas `product_id IS NULL` sin el flag (mostrador),
#: un OFFSET creciente re-escanea desde cero en cada página.
_QUEUE_PAGE_SIZE = 1000

#: Tope de filas que SÍ califican (tienen `_unlinked_product_name_raw`) antes
#: de cortar. No es una regla de negocio — es memoria acotada.
_QUEUE_MAX_MATCHES = 5000

#: Tope de filas ESCANEADAS en total, aunque casi ninguna califique. Sin esto,
#: un tenant con muchas ventas `product_id IS NULL` sin el flag (mostrador)
#: haría escanear la tabla entera para juntar unos pocos grupos reales.
_QUEUE_MAX_SCAN = 50_000

_CANDIDATE_STOPWORDS = frozenset(
    {"de", "del", "la", "el", "los", "las", "un", "una", "para", "con", "sin", "por"}
)


async def _scan_unlinked_sale_entries(
    session: AsyncSession,
    tenant_id: UUID,
    *,
    raw_name: str | None = None,
) -> tuple[list[SaleEntry], bool]:
    """Escanea `SaleEntry` con `product_id IS NULL`, ordenado por `id`, en
    páginas de `_QUEUE_PAGE_SIZE`, hasta juntar `_QUEUE_MAX_MATCHES` filas que
    tengan `_unlinked_product_name_raw` (o agotar el tenant), sin cargar más
    de `_QUEUE_MAX_SCAN` filas en memoria de una.

    Con `raw_name`, sólo junta las de ESE grupo (para el POST de vinculación).
    Sin él, junta TODAS las que califican (para el GET, que las agrupa después).

    Devuelve `(entries, truncated)`. `truncated=True` sólo si se COMPROBÓ que
    queda al menos una fila más que calificaba y no entró — nunca se asume por
    llegar justo al tope: seguir escaneando (sin agregar a `matches`, ya
    lleno) es la única forma de distinguir "el tope coincidió con el final"
    de "el tope cortó en medio de más filas". Nunca en silencio.
    """
    matches: list[SaleEntry] = []
    scanned = 0
    last_id: UUID | None = None
    hay_mas_alla_del_tope = False
    while True:
        stmt = (
            select(SaleEntry)
            .where(
                SaleEntry.tenant_id == tenant_id,
                SaleEntry.product_id.is_(None),
                SaleEntry.voided_at.is_(None),
            )
            .order_by(SaleEntry.id)
            .limit(_QUEUE_PAGE_SIZE)
        )
        if last_id is not None:
            stmt = stmt.where(SaleEntry.id > last_id)
        page = (await session.execute(stmt)).scalars().all()
        if not page:
            break
        page_llena = len(page) == _QUEUE_PAGE_SIZE
        for entry in page:
            scanned += 1
            name = (entry.custom_fields or {}).get(UNLINKED_PRODUCT_NAME_FIELD)
            if not name or (raw_name is not None and name != raw_name):
                continue
            if len(matches) < _QUEUE_MAX_MATCHES:
                matches.append(entry)
            else:
                # Ya está lleno: esta fila califica igual, así que SÍ queda
                # más — no es una suposición.
                hay_mas_alla_del_tope = True
        last_id = page[-1].id
        if hay_mas_alla_del_tope:
            break
        if scanned >= _QUEUE_MAX_SCAN:
            # No se llegó a comprobar si hay más — tope de trabajo, no de
            # datos. Conservador: si la página vino llena, puede haber más.
            hay_mas_alla_del_tope = page_llena
            break
        if not page_llena:
            break  # se acabaron las filas `product_id IS NULL` del tenant
    return matches, hay_mas_alla_del_tope


async def _suggest_product_candidates(
    session: AsyncSession, tenant_id: UUID, raw_name: str, *, limit: int = 5
) -> list[ProductLinkCandidate]:
    """Candidatos livianos por nombre, misma FORMA que `match_candidates` en
    `/otros` (F2-T2b) para que un frontend futuro reuse el mismo componente —
    pero sin reusar el motor de identidad privado del import
    (`ingestion_import_service`), que no está pensado para invocarse fuera de
    una corrida. Búsqueda por token, `ILIKE` (portable SQLite/Postgres vía
    `Column.ilike`), acotada a 3 tokens y `limit` resultados. Puramente
    orientativa: la decisión la sigue tomando la persona.
    """
    tokens = [
        t
        for t in re.split(r"\s+", raw_name.strip().lower())
        if len(t) >= 4 and t not in _CANDIDATE_STOPWORDS
    ]
    if not tokens:
        return []
    conditions = [Product.name.ilike(f"%{t}%") for t in tokens[:3]]
    result = await session.execute(
        select(Product)
        .where(
            Product.tenant_id == tenant_id,
            Product.is_active.is_(True),
            or_(*conditions),
        )
        .limit(limit)
    )
    return [
        ProductLinkCandidate(
            id=p.id, matched_by=["name"], name=p.name, sku=p.sku, barcode=p.barcode
        )
        for p in result.scalars().all()
    ]


@router.get(
    "/product-link-queue",
    response_model=ProductLinkQueueResponse,
    summary="Ventas sin producto vinculado, agrupadas por nombre",
)
async def get_product_link_queue(
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> ProductLinkQueueResponse:
    entries, truncated = await _scan_unlinked_sale_entries(session, tenant.tenant_id)

    grouped: dict[str, list[UUID]] = {}
    for entry in entries:
        name = entry.custom_fields[UNLINKED_PRODUCT_NAME_FIELD]  # presente: ya filtrado arriba
        grouped.setdefault(name, []).append(entry.id)

    groups = [
        ProductLinkQueueGroup(
            raw_name=name,
            count=len(ids),
            sample_sale_ids=ids[:5],
            candidates=await _suggest_product_candidates(session, tenant.tenant_id, name),
        )
        # Orden estable: cantidad descendente, nombre como desempate — para que
        # el orden no dependa de en qué página cayó cada grupo.
        for name, ids in sorted(grouped.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    ]
    return ProductLinkQueueResponse(groups=groups, truncated=truncated)


@router.post(
    "/product-link-queue/link",
    response_model=LinkProductQueueResponse,
    summary="Vincula en bloque todas las ventas con ese nombre crudo a un producto",
)
async def link_product_queue_group(
    body: LinkProductQueueRequest,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(require_modify_access),
    _maintenance_guard: None = Depends(ensure_tenant_not_under_maintenance),
) -> LinkProductQueueResponse:
    # Re-validación: NUNCA confiar en el id que manda el cliente (mismo
    # criterio que `others.py::reclassify_record`, rama "vincular a existente").
    target = await session.get(Product, body.target_product_id)
    if target is None or target.tenant_id != tenant.tenant_id or not target.is_active:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_TARGET_PRODUCT"},
        )

    # Muta un producto EXISTENTE (`custom_fields`) — mismo chokepoint que el
    # resto de las mutaciones de catálogo fuera de `ProductRepository.save()`
    # (`products.py`), para que el dedup (que toma el exclusive) no corra en
    # simultáneo con esto.
    await maintenance_lock_service.acquire_write_lock_shared(session, tenant.tenant_id)

    entries, truncated = await _scan_unlinked_sale_entries(
        session, tenant.tenant_id, raw_name=body.raw_name
    )
    linked_ids: list[UUID] = []
    for entry in entries:
        entry.product_id = target.id
        entry.custom_fields = {
            k: v for k, v in (entry.custom_fields or {}).items()
            if k != UNLINKED_PRODUCT_NAME_FIELD
        }
        # F-O/F-F: una relectura del archivo original no puede pisar esta
        # decisión humana — mismo guard que el PATCH manual (línea ~530).
        if entry.source_upload_id is not None:
            entry.has_user_edits = True
        linked_ids.append(entry.id)

    if linked_ids:
        target.custom_fields = add_alias(target.custom_fields, body.raw_name)
        # Una fila de auditoría por OPERACIÓN, no por venta — miles de filas
        # por un solo click sería ruido, no trazabilidad. `user` de
        # `require_modify_access` es quien decidió el vínculo.
        session.add(
            DecisionAuditLog(
                tenant_id=tenant.tenant_id,
                decision_type="SALES_PRODUCT_BULK_LINKED",
                decision_data={
                    "raw_name": body.raw_name,
                    "target_product_id": str(target.id),
                    "linked_count": len(linked_ids),
                    "sample_sale_ids": [str(i) for i in linked_ids[:20]],
                    "alias_added": body.raw_name,
                },
                triggered_by="ui:data_records",
                actor_user_id=user.user_id,
                context={"endpoint": "sales/product-link-queue"},
                created_at=datetime.now(UTC),
            )
        )

    await session.flush()
    trigger_score_recalculation.delay(str(tenant.tenant_id), "sales_product_bulk_linked")
    return LinkProductQueueResponse(linked=len(linked_ids), truncated=truncated)


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
    user: User = Depends(require_modify_access),
    session: AsyncSession = Depends(get_db_session),
) -> SaleEntry:
    repo = SaleRepository(session)
    entry = await repo.get_by_id(sale_id, tenant.tenant_id)
    if not entry:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sale not found.")
    before = _sale_snapshot(entry)
    _prev_product_id = entry.product_id
    _prev_quantity = entry.quantity
    # Efecto en stock SOLO para ventas EN VIVO (source_upload_id is None) y solo si cambia
    # producto o cantidad. Se computa desde el body ANTES de mutar la entry para poder
    # validar (NO stock negativo) sin dejar estado sucio si se rechaza.
    _new_product_id = body.product_id if "product_id" in body.model_fields_set else _prev_product_id
    _new_quantity = body.quantity if body.quantity is not None else _prev_quantity
    _is_live_sale = entry.source_upload_id is None
    _stock_changed = _new_product_id != _prev_product_id or _new_quantity != _prev_quantity
    if _is_live_sale and _stock_changed:
        await stock_service.validate_sale_update_stock(
            entry.id,
            tenant.tenant_id,
            _prev_product_id,
            _prev_quantity,
            _new_product_id,
            _new_quantity,
            session,
        )
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
    # Ajuste de stock (ya validado arriba, no puede dejar negativo). Una venta histórica
    # importada/releída (source_upload_id presente) NO arranca a descontar por editarla:
    # su cantidad ya la cuenta la integridad desde sales_entries; descontar acá duplicaría.
    # Efecto neto: reponer el movimiento viejo (si había) y descontar con los valores
    # nuevos. decrement_for_sale no-opea si se desasoció el producto (product_id None) → en
    # ese caso solo repone stock.
    if _is_live_sale and _stock_changed:
        await stock_service.revert_sale_stock(entry.id, tenant.tenant_id, session)
        await stock_service.decrement_for_sale(entry, session)
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
    user: User = Depends(require_modify_access),
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
    # Anular la venta repone el stock que había descontado (incremental). No-op si la
    # venta no tenía movimiento (sin producto o histórica importada). Cierra el bug
    # latente donde voidar una venta de manual-batch dejaba el stock bajo para siempre.
    await stock_service.revert_sale_stock(entry.id, tenant.tenant_id, session)
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
