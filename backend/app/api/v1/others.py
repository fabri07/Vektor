"""Sección "Otros" — bandeja de revisión de datos no clasificados.

Todo lo que llegó por chat/ingesta/reanálisis y no se clasificó como
venta/gasto/producto queda en ``unclassified_records``. Desde acá el tenant:
  - lista lo pendiente (`GET /others`),
  - lo importa como venta/gasto/producto (`POST /others/{id}/reclassify`,
    valida con los mismos schemas que los endpoints de creación), o
  - lo descarta (`POST /others/{id}/dismiss`), o
  - descarta un GRUPO entero (B·5a: `GET /others/summary` →
    `POST /others/dismiss-group/preview` → `POST /others/dismiss-group`).
"""

import base64
import hashlib
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1._stock_manual import (
    exigir_fecha_con_stock,
    momento_del_costo_del_alta,
    registrar_apertura_http,
)
from app.api.v1.deps import (
    ensure_tenant_not_under_maintenance,
    get_current_tenant,
    require_active_subscription,
    require_modify_access,
    require_role,
)
from app.api.v1.expenses import _apply_category_label
from app.api.v1.products import (
    _duplicate_identity_conflict,
    _find_active_product_by_identity,
    _identity_conflict_from_db,
    _tenant_vertical,
)
from app.application.services import compra_service, maintenance_lock_service
from app.application.services._ledger_restore import snapshot_master
from app.application.services.file_deletion_service import (
    ACTION_CREATE_CUSTOMER,
    ACTION_CREATE_PRODUCT,
    ACTION_CREATE_SUPPLIER,
    ACTION_UPDATE_PRODUCT,
    REPAIR_TYPE_IMPORT,
)
from app.application.services.ingestion_import_service import IDENTITY_REF_KEY
from app.application.services.operation_identity_service import identidad_tomada
from app.application.services.product_identity import (
    ProductIdentityConflictError,
    product_identity_guard,
)
from app.application.services.score_trigger_service import (
    trigger_score_recalculation_after_commit,
)
from app.domain.business_time import (
    a_hora_de_negocio,
    fecha_de_negocio,
    now_ar_naive,
    today_ar,
)
from app.domain.comprobante_de_compra import (
    MOTIVO_COMPRA_SIN_ARTICULOS,
    ComprobanteInvalidoError,
    Envio,
    Iva,
    LineaDeCompra,
    ModoEnvio,
    TratamientoIva,
)
from app.domain.costo_vigente import (
    conservar_claves_del_costo,
    custom_fields_tras_editar_costo,
    snapshot_claves_del_costo,
)
from app.domain.expense_categories import (
    EXPENSE_CATEGORY_LABELS_ES,
    classify_expense_with_vertical,
    infer_expense_type,
    normalize_expense_category,
    resolve_tax_kind,
)
from app.domain.product_categories import (
    PRODUCT_CATEGORY_LABELS,
    normalize_product_category,
)
from app.domain.product_completion import recompute_requires_completion
from app.domain.sale_unit import a_unidades_base
from app.persistence.db.session import get_db_session
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.customer import Customer
from app.persistence.models.file import UploadedFile
from app.persistence.models.product import Product
from app.persistence.models.repair import DataRepairItem, DataRepairRun
from app.persistence.models.supplier import Supplier
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.persistence.models.unclassified_record import (
    UNCLASSIFIED_STATUS_DISMISSED,
    UNCLASSIFIED_STATUS_IMPORTED,
    UNCLASSIFIED_STATUS_PENDING,
    UnclassifiedRecord,
    unclassified_row_ref,
)
from app.persistence.models.user import User
from app.persistence.repositories.product_repository import ProductRepository
from app.schemas.common import MessageResponse
from app.schemas.customer import CreateCustomerRequest
from app.schemas.product import CreateProductRequest, UpdateProductRequest
from app.schemas.purchase import PurchaseShipping, PurchaseTax
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
    # E6b: la fila llegó a "Otros" porque tiene el mismo comprobante que una
    # operación ya cargada, con datos distintos. Importarla desde acá sin volver a
    # mirar el candado convertiría la revisión humana en la forma de saltearse la
    # protección: se aplicaría el efecto duplicado que el importador se negó a
    # aplicar. Sin este flag el endpoint responde 409; con él, el usuario declara
    # que revisó y que la quiere igual.
    aplicar_pese_al_conflicto: bool = False


class ResolvePurchaseLine(BaseModel):
    product_id: UUID
    quantity: int = Field(ge=1)
    #: Lo que dice el comprobante para esta línea (ver `domain/comprobante_de_compra`).
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)


class ResolvePurchaseRequest(BaseModel):
    """B·5b: la compra se resuelve diciendo QUÉ artículos la componen (`lines`).

    El body de un solo producto (`target_product_id` + `amount` + `quantity`) se
    sigue aceptando para un frontend viejo durante el deploy en paralelo: es una
    compra de una línea, con envío e IVA desconocidos como antes.
    """

    lines: list[ResolvePurchaseLine] | None = Field(default=None, min_length=1, max_length=200)
    # Body viejo (una línea).
    target_product_id: UUID | None = None
    amount: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    quantity: int | None = Field(default=None, gt=0)
    unit_cost: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    transaction_date: datetime
    payment_method: str = Field(pattern=PAYMENT_METHOD_PATTERN, default="transfer")
    category: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    supplier_name: str | None = Field(default=None, max_length=300)
    shipping: PurchaseShipping | None = None
    tax: PurchaseTax | None = None
    document_ref: str | None = Field(default=None, max_length=100)
    # E6b: misma regla que `reclassify` para una fila con el comprobante tomado.
    aplicar_pese_al_conflicto: bool = False

    @field_validator("transaction_date")
    @classmethod
    def transaction_date_not_future(cls, value: datetime) -> datetime:
        if fecha_de_negocio(value) > today_ar():
            raise ValueError("transaction_date cannot be in the future.")
        return a_hora_de_negocio(value)


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
    # B·5a: la clave opaca de `GET /others/summary` para ver un solo grupo.
    group: str | None = Query(default=None, max_length=4000),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> list[UnclassifiedRecordResponse]:
    # Lazy import: ingestion_import_service importa schemas/transaction y crearía
    # un ciclo a nivel módulo (mismo criterio que bulk_import_unclassified).
    from app.application.services.ingestion_import_service import (  # noqa: PLC0415
        _row_val_categoria,
    )

    filtros = (
        _group_filter(tenant.tenant_id, group, record_status)
        if group
        else [
            UnclassifiedRecord.tenant_id == tenant.tenant_id,
            UnclassifiedRecord.status == record_status,
        ]
    )
    q = (
        select(UnclassifiedRecord)
        .where(*filtros)
        .order_by(UnclassifiedRecord.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(q)
    records = list(result.scalars().all())

    vertical = await _tenant_vertical(session, tenant.tenant_id)
    responses: list[UnclassifiedRecordResponse] = []
    for rec in records:
        resp = UnclassifiedRecordResponse.model_validate(rec)
        # Ocultar claves internas reservadas (prefijo ``__``, p.ej. la correlación
        # ``__risk_ref__`` de F8b): son metadata, no columnas del archivo.
        resp.row_data = {
            k: v for k, v in (rec.row_data or {}).items() if not k.startswith("__")
        }
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
                vertical
            ].get(code, code)
        responses.append(resp)
    return responses


# ── B·5a: «Otros» por grupo ──────────────────────────────────────────────────
#
# Un grupo es lo que comparte archivo, hoja (`context_id`), motivo
# (`context_label`) y sugerencia de destino. El motivo agrupa —«Fila de total»,
# «Venta sin stock que la respalde»— pero no identifica una hoja: por eso la
# hoja va aparte, y una captura vieja sin `context_id` forma su propio grupo.
# El cliente no arma la clave: la recibe opaca del resumen y la devuelve.

_GROUP_IDS_EN_AUDITORIA = 200
_GROUP_MUESTRAS = 5


def _encode_group_key(
    uploaded_file_id: UUID | None,
    context_id: str | None,
    context_label: str | None,
    suggested_entity: str | None,
) -> str:
    raw = json.dumps(
        [
            str(uploaded_file_id) if uploaded_file_id else None,
            context_id,
            context_label,
            suggested_entity,
        ],
        ensure_ascii=False,
    )
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_group_key(key: str) -> tuple[UUID | None, str | None, str | None, str | None]:
    try:
        padded = key + "=" * (-len(key) % 4)
        file_raw, ctx, label, entity = json.loads(base64.urlsafe_b64decode(padded))
        if file_raw is not None and not isinstance(file_raw, str):
            raise TypeError("uploaded_file_id")
        file_id = UUID(file_raw) if file_raw else None
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_GROUP_KEY"},
        ) from exc
    for part in (ctx, label, entity):
        if part is not None and not isinstance(part, str):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "INVALID_GROUP_KEY"},
            )
    return file_id, ctx, label, entity


def _group_filter(tenant_id: uuid.UUID, key: str, record_status: str) -> list[Any]:
    file_id, ctx, label, entity = _decode_group_key(key)

    def _eq(col: Any, value: Any) -> Any:
        return col.is_(None) if value is None else col == value

    return [
        UnclassifiedRecord.tenant_id == tenant_id,
        UnclassifiedRecord.status == record_status,
        _eq(UnclassifiedRecord.uploaded_file_id, file_id),
        _eq(UnclassifiedRecord.context_id, ctx),
        _eq(UnclassifiedRecord.context_label, label),
        _eq(UnclassifiedRecord.suggested_entity, entity),
    ]


def _group_token(ids: list[UUID]) -> str:
    """Huella del CONJUNTO exacto de filas, no de su cantidad: si salen dos y
    entran otras dos entre la vista previa y el descarte, el conteo es el mismo
    y el token no."""
    return hashlib.sha256(",".join(sorted(str(i) for i in ids)).encode()).hexdigest()


def _visible_row(row: dict[str, Any] | None) -> dict[str, Any]:
    return {k: v for k, v in (row or {}).items() if not k.startswith("__")}


class OthersGroupResponse(BaseModel):
    group_key: str
    uploaded_file_id: UUID | None
    file_name: str | None
    context_id: str | None
    motivo: str | None
    suggested_entity: str | None
    status: str
    count: int


class DismissGroupPreviewRequest(BaseModel):
    group_key: str = Field(min_length=1, max_length=4000)


class DismissGroupPreviewResponse(BaseModel):
    count: int
    samples: list[dict[str, Any]]
    group_token: str


class DismissGroupRequest(BaseModel):
    group_key: str = Field(min_length=1, max_length=4000)
    group_token: str = Field(min_length=64, max_length=64)


class DismissGroupResponse(BaseModel):
    dismissed: int


@router.get(
    "/summary",
    response_model=list[OthersGroupResponse],
    summary="Otros agrupado por archivo, hoja, motivo y sugerencia",
)
async def others_summary(
    record_status: str = Query(default=UNCLASSIFIED_STATUS_PENDING, alias="status"),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> list[OthersGroupResponse]:
    """El conteo de cada grupo es el del grupo COMPLETO, no el de una página."""
    rows = (
        await session.execute(
            select(
                UnclassifiedRecord.uploaded_file_id,
                UnclassifiedRecord.context_id,
                UnclassifiedRecord.context_label,
                UnclassifiedRecord.suggested_entity,
                func.count(UnclassifiedRecord.id),
                func.max(UnclassifiedRecord.created_at),
                UploadedFile.original_filename,
            )
            .outerjoin(
                UploadedFile,
                (UploadedFile.id == UnclassifiedRecord.uploaded_file_id)
                & (UploadedFile.tenant_id == UnclassifiedRecord.tenant_id),
            )
            .where(
                UnclassifiedRecord.tenant_id == tenant.tenant_id,
                UnclassifiedRecord.status == record_status,
            )
            .group_by(
                UnclassifiedRecord.uploaded_file_id,
                UnclassifiedRecord.context_id,
                UnclassifiedRecord.context_label,
                UnclassifiedRecord.suggested_entity,
                UploadedFile.original_filename,
            )
            .order_by(func.max(UnclassifiedRecord.created_at).desc())
        )
    ).all()
    return [
        OthersGroupResponse(
            group_key=_encode_group_key(file_id, ctx, label, entity),
            uploaded_file_id=file_id,
            file_name=file_name,
            context_id=ctx,
            motivo=label,
            suggested_entity=entity,
            status=record_status,
            count=int(count),
        )
        for file_id, ctx, label, entity, count, _ultimo, file_name in rows
    ]


@router.post(
    "/dismiss-group/preview",
    response_model=DismissGroupPreviewResponse,
    summary="Vista previa del descarte de un grupo de Otros",
)
async def dismiss_group_preview(
    body: DismissGroupPreviewRequest,
    tenant: Tenant = Depends(get_current_tenant),
    # El MISMO permiso que el descarte: quien no puede descartar no tiene por qué
    # ver una confirmación que después le da 403 (el PIN se pide acá, una vez).
    _: User = Depends(require_modify_access),
    session: AsyncSession = Depends(get_db_session),
) -> DismissGroupPreviewResponse:
    filtros = _group_filter(tenant.tenant_id, body.group_key, UNCLASSIFIED_STATUS_PENDING)
    ids = list((await session.execute(select(UnclassifiedRecord.id).where(*filtros))).scalars())
    muestras = (
        await session.execute(
            select(UnclassifiedRecord.row_data)
            .where(*filtros)
            .order_by(UnclassifiedRecord.created_at)
            .limit(_GROUP_MUESTRAS)
        )
    ).scalars()
    return DismissGroupPreviewResponse(
        count=len(ids),
        samples=[_visible_row(r) for r in muestras],
        group_token=_group_token(ids),
    )


@router.post(
    "/dismiss-group",
    response_model=DismissGroupResponse,
    summary="Descartar un grupo de Otros (exactamente las filas de la vista previa)",
)
async def dismiss_group(
    body: DismissGroupRequest,
    tenant: Tenant = Depends(get_current_tenant),
    # PIN antes que el guard 423 (mismo orden que el resto de las escrituras).
    user: User = Depends(require_modify_access),
    _maintenance_guard: None = Depends(ensure_tenant_not_under_maintenance),
    session: AsyncSession = Depends(get_db_session),
) -> DismissGroupResponse:
    """Descarta las filas del grupo SÓLO si son exactamente las de la vista previa.

    Se toman con `FOR UPDATE` y se recalcula el token sobre lo que hay ahora: si
    entre la vista previa y la confirmación entró o salió una fila (un import, una
    relectura, otra sesión que resolvió una), el conjunto ya no es el que el
    usuario vio y se responde 409 sin tocar nada.
    """
    records = list(
        (
            await session.execute(
                select(UnclassifiedRecord)
                .where(
                    *_group_filter(
                        tenant.tenant_id, body.group_key, UNCLASSIFIED_STATUS_PENDING
                    )
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    if not records or _group_token([r.id for r in records]) != body.group_token:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "GROUP_CHANGED",
                "message": (
                    "El grupo cambió desde que lo revisaste. Volvé a abrir la "
                    "vista previa antes de descartarlo."
                ),
            },
        )
    ahora = datetime.now(UTC)
    for rec in records:
        rec.status = UNCLASSIFIED_STATUS_DISMISSED
        rec.resolved_at = ahora
    file_id, ctx, label, entity = _decode_group_key(body.group_key)
    session.add(
        DecisionAuditLog(
            tenant_id=tenant.tenant_id,
            decision_type="OTHERS_GROUP_DISMISSED",
            decision_data={
                "uploaded_file_id": str(file_id) if file_id else None,
                "context_id": ctx,
                "motivo": label,
                "suggested_entity": entity,
                "cantidad": len(records),
                "group_token": body.group_token,
                "record_ids": [str(r.id) for r in records[:_GROUP_IDS_EN_AUDITORIA]],
            },
            triggered_by="ui:others_dismiss_group",
            actor_user_id=user.user_id,
            created_at=ahora,
        )
    )
    await session.flush()
    return DismissGroupResponse(dismissed=len(records))


@router.get("/count", summary="Cantidad de registros pendientes en Otros")
async def count_unclassified(
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, int]:
    """``pending`` y, de esos, cuántos tienen sugerencia de destino.

    ``pending_suggested`` existe porque "Importar todo lo sugerido" opera sobre
    TODOS los pendientes del tenant, no sobre la página que se está viendo: sin
    un conteo global, el frontend tendría que decidir si habilitar el botón
    mirando 50 filas de 2.288 y se equivocaría en las dos direcciones.
    """
    result = await session.execute(
        select(
            func.count(UnclassifiedRecord.id),
            func.count(UnclassifiedRecord.suggested_entity),
        ).where(
            UnclassifiedRecord.tenant_id == tenant.tenant_id,
            UnclassifiedRecord.status == UNCLASSIFIED_STATUS_PENDING,
        )
    )
    pending, suggested = result.one()
    return {"pending": int(pending or 0), "pending_suggested": int(suggested or 0)}


def _snapshot_producto(producto: Product) -> dict[str, Any]:
    """Estado restaurable de un producto, en el formato del ledger.

    Mismas claves que produce el importador, para que la reversa lea un único
    formato venga del confirm o de una clasificación manual.
    """
    return {
        "sale_price_ars": str(producto.sale_price_ars),
        "list_price_ars": (
            str(producto.list_price_ars) if producto.list_price_ars is not None else None
        ),
        "unit_cost_ars": (
            str(producto.unit_cost_ars) if producto.unit_cost_ars is not None else None
        ),
        "sku": producto.sku,
        "barcode": producto.barcode,
        "category": producto.category,
        "updated_at": producto.updated_at.isoformat() if producto.updated_at else None,
        # M·1c: la procedencia del costo vuelve junto con el costo.
        **snapshot_claves_del_costo(producto.custom_fields),
    }


async def _ledger_desde_otros(
    session: AsyncSession,
    tenant_id: UUID,
    upload_id: UUID | None,
    *,
    action: str,
    product_id: UUID | None,
    before: dict[str, Any] | None,
    after: dict[str, Any],
) -> None:
    """Registra en el ledger lo que produjo clasificar una fila de "Otros".

    ``Product``/``Customer``/``Supplier`` no tienen columna de origen: el vínculo
    con el archivo SOLO puede vivir acá. Sin esto, clasificar a mano una fila
    generaba una entidad huérfana que sobrevivía al borrado del archivo.

    Sin ``upload_id`` no se escribe nada: una fila sin archivo de origen (carga
    manual) no tiene procedencia que registrar.
    """
    if upload_id is None:
        return
    run = DataRepairRun(
        tenant_id=tenant_id,
        repair_type=REPAIR_TYPE_IMPORT,
        status="APPLIED",
        dry_run=False,
        details_json={"file_id": str(upload_id), "origen": "otros:reclassify"},
        completed_at=datetime.now(UTC),
    )
    session.add(run)
    await session.flush()
    session.add(
        DataRepairItem(
            run_id=run.id,
            tenant_id=tenant_id,
            source_file_id=upload_id,
            product_id=product_id,
            action=action,
            before_json=before,
            after_json=after,
            confidence="HIGH",
        )
    )
    await session.flush()


async def _ledger_maestro_creado(
    session: AsyncSession, tenant_id: UUID, upload_id: UUID | None, entity: Any, kind: str
) -> None:
    if upload_id is None:
        return
    await session.flush()
    await session.refresh(entity)
    await _ledger_desde_otros(
        session,
        tenant_id,
        upload_id,
        action=(ACTION_CREATE_CUSTOMER if kind == "customer" else ACTION_CREATE_SUPPLIER),
        product_id=None,
        before=None,
        after=snapshot_master(entity, kind),
    )


async def _ledger_producto_creado(
    session: AsyncSession, tenant_id: UUID, upload_id: UUID | None, producto: Product
) -> None:
    if upload_id is None:
        return
    await session.refresh(producto)
    await _ledger_desde_otros(
        session,
        tenant_id,
        upload_id,
        action=ACTION_CREATE_PRODUCT,
        product_id=producto.id,
        before=None,
        after=_snapshot_producto(producto),
    )


async def _ledger_producto_actualizado(
    session: AsyncSession,
    tenant_id: UUID,
    upload_id: UUID | None,
    producto: Product,
    before: dict[str, Any],
) -> None:
    if upload_id is None:
        return
    await _ledger_desde_otros(
        session,
        tenant_id,
        upload_id,
        action=ACTION_UPDATE_PRODUCT,
        product_id=producto.id,
        before=before,
        after=_snapshot_producto(producto),
    )


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
            detail={
                "code": "RECORD_ALREADY_RESOLVED",
                "message": "El registro ya fue importado o descartado.",
            },
        )
    return record


@router.post(
    "/{record_id}/reclassify",
    dependencies=[Depends(require_active_subscription)],
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
    user: User = Depends(require_role("OWNER", "ADMIN")),
    _maintenance_guard: None = Depends(ensure_tenant_not_under_maintenance),
    session: AsyncSession = Depends(get_db_session),
) -> MessageResponse:
    # B·5a: con `FOR UPDATE`, igual que el descarte (de a uno y por grupo): si no,
    # una fila que otra sesión acaba de descartar se importaría igual y su estado
    # quedaría pisado con IMPORTED.
    record = await _get_pending_record(
        session, tenant.tenant_id, record_id, for_update=True
    )

    # Procedencia de la fila: sin esto, el registro que nace de clasificar a mano
    # una fila de "Otros" queda HUÉRFANO de su archivo — borrar el archivo no lo
    # alcanza y sobrevive sin rastro de dónde vino.
    #
    # `source_row_ref` se deriva del id del UnclassifiedRecord, no de sus valores:
    # es estable aunque el usuario corrija un campo antes de clasificar.
    _upload_id = record.uploaded_file_id
    _row_ref = unclassified_row_ref(record.id) if _upload_id else None

    # E6b — el candado también rige acá. Una fila que quedó en "Otros" por
    # conflicto de identidad guarda su clave; si esa clave sigue tomada por una
    # operación viva, importarla duplicaría el efecto que el confirm no aplicó.
    # Se exige una decisión explícita en vez de resolverlo solo: puede ser una
    # corrección legítima (el proveedor reemitió), y Véktor no puede saber cuál
    # de las dos versiones vale.
    _clave_identidad = str(record.row_data.get(IDENTITY_REF_KEY) or "")
    if (
        _clave_identidad
        and body.entity_type in ("sale", "expense")
        and not body.aplicar_pese_al_conflicto
        and await identidad_tomada(session, tenant.tenant_id, _clave_identidad) is not None
    ):
        # Detalle ESTRUCTURADO y no un string: el frontend rutea por
        # `detail.code`, y sin código este 409 caería en el catch-all "Revisá los
        # campos" — que apunta al lugar equivocado. Acá los campos están bien; lo
        # que hay es una decisión pendiente.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "IMPORT_IDENTITY_TAKEN",
                "message": (
                    "Esta fila tiene el mismo comprobante que una operación ya "
                    "cargada. Si la importás igual vas a tener las dos: revisá "
                    "cuál corresponde."
                ),
            },
        )

    try:
        if body.entity_type == "sale":
            sale_req = CreateSaleRequest(**body.fields)
            # La fila es de un DOCUMENTO: su cantidad está en la unidad de venta
            # («2» de queso son 2 kg). Misma regla que «Importar sugeridos».
            _cantidad = sale_req.quantity
            if sale_req.product_id is not None:
                _vendido = await session.get(Product, sale_req.product_id)
                if _vendido is not None and _vendido.tenant_id == tenant.tenant_id:
                    _cantidad = a_unidades_base(
                        sale_req.quantity, _vendido.base_units_per_sale_unit or 1
                    )
            session.add(
                SaleEntry(
                    tenant_id=tenant.tenant_id,
                    amount=sale_req.amount,
                    quantity=_cantidad,
                    transaction_date=sale_req.transaction_date,
                    payment_method=sale_req.payment_method,
                    product_id=sale_req.product_id,
                    notes=sale_req.notes,
                    custom_fields=sale_req.custom_fields,
                    provenance="REAL",
                    source_upload_id=_upload_id,
                    source_row_ref=_row_ref,
                )
            )
            label = "venta"
        elif body.entity_type == "expense":
            # Normalizar la categoría libre antes de validar contra el catálogo.
            raw_fields = dict(body.fields)
            raw_category = raw_fields.get("category")
            code, cat_label = normalize_expense_category(raw_category)
            if code == "INVENTORY":
                # B·5b: una compra de mercadería sin artículos no es un dato útil.
                # Se resuelve con `resolve-purchase`, diciendo qué productos y cuántos.
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={
                        "code": "PURCHASE_NEEDS_ITEMS",
                        "message": MOTIVO_COMPRA_SIN_ARTICULOS,
                    },
                )
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
                    tax_kind=resolve_tax_kind(
                        exp_req.category, explicit=exp_req.tax_kind,
                        textos=(str(raw_category) if raw_category else None,),
                    ),
                    transaction_date=exp_req.expense_date,
                    description=exp_req.description,
                    is_recurring=exp_req.is_recurring,
                    payment_method=exp_req.payment_method,
                    supplier_name=exp_req.supplier_name,
                    notes=exp_req.notes,
                    custom_fields=custom_fields,
                    provenance="REAL",
                    source_upload_id=_upload_id,
                    source_row_ref=_row_ref,
                )
            )
            label = "gasto"
        elif body.entity_type == "customer":
            cust_req = CreateCustomerRequest(**body.fields)
            _nuevo_cliente = Customer(tenant_id=tenant.tenant_id, **cust_req.model_dump())
            session.add(_nuevo_cliente)
            # `Customer` no tiene columna de origen: su procedencia sólo puede
            # vivir en el ledger, igual que la de los productos.
            await _ledger_maestro_creado(
                session, tenant.tenant_id, _upload_id, _nuevo_cliente, "customer"
            )
            label = "cliente"
        elif body.entity_type == "supplier":
            sup_req = CreateSupplierRequest(**body.fields)
            _nuevo_prov = Supplier(tenant_id=tenant.tenant_id, **sup_req.model_dump())
            session.add(_nuevo_prov)
            await _ledger_maestro_creado(
                session, tenant.tenant_id, _upload_id, _nuevo_prov, "supplier"
            )
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
            # `before` ANTES de mutar: esta rama pisa precio/costo de un producto
            # PREEXISTENTE y hasta acá no dejaba ningún rastro, así que borrar el
            # archivo no podía devolverle su valor.
            _before_target = _snapshot_producto(target)
            # M·1c: un costo tipeado acá es una edición manual con su hora.
            if "unit_cost_ars" in link_updates:
                _cf_costo = custom_fields_tras_editar_costo(
                    target.custom_fields,
                    target.unit_cost_ars,
                    link_updates["unit_cost_ars"],
                    now_ar_naive(),
                )
                if _cf_costo is not None:
                    target.custom_fields = _cf_costo
            for field_name, value in link_updates.items():
                setattr(target, field_name, value)
            recompute_requires_completion(target)
            await session.flush()
            await session.refresh(target)
            await _ledger_producto_actualizado(
                session, tenant.tenant_id, _upload_id, target, _before_target
            )
            record.status = UNCLASSIFIED_STATUS_IMPORTED
            record.resolved_at = datetime.now(UTC)
            await session.flush()
            trigger_score_recalculation_after_commit(
                session, str(tenant.tenant_id), "unclassified_reclassified"
            )
            return MessageResponse(message="Registro vinculado al producto existente.")
        else:  # "product", crear nuevo
            # CRITICAL (F3-T3 review): shared lock ANTES de mutar catálogo — el
            # session.add(Product(...)) de abajo NO pasa por ProductRepository.save(),
            # así que no queda cubierto por el acquire de ese chokepoint.
            await maintenance_lock_service.acquire_write_lock_shared(session, tenant.tenant_id)
            prod_req = CreateProductRequest(**body.fields)
            data = prod_req.model_dump()
            # M·P2c: el stock inicial entra como apertura en el ledger (igual
            # que POST /products), no fijado en la fila. Acá NO se exige el
            # costo: la fila viene de un archivo, y una fila importada sin costo
            # entra igual (queda `incompleto`), como en el import.
            stock_inicial = data.pop("stock_units")
            costo_envio = data.pop("costo_envio")
            data["stock_units"] = 0
            # M·2d: la fecha del stock inicial la da el usuario, como en el alta.
            exigir_fecha_con_stock(stock_inicial > 0, data.get("acquired_at"))
            existing_product = await _find_active_product_by_identity(
                session, tenant.tenant_id, barcode=data.get("barcode"), sku=data.get("sku")
            )
            if existing_product is not None:
                raise _duplicate_identity_conflict(
                    existing_product, barcode=data.get("barcode"), sku=data.get("sku")
                )
            # Misma normalización que POST /products (catálogo del vertical).
            if data.get("category"):
                vertical = await _tenant_vertical(session, tenant.tenant_id)
                code_p, label_p = normalize_product_category(data["category"], vertical)
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
                    # M·1c: las claves del costo las escribe el servidor; el
                    # costo tipeado queda como edición manual con su hora.
                    _cf_nuevo = conservar_claves_del_costo(None, data.get("custom_fields"))
                    data["custom_fields"] = (
                        custom_fields_tras_editar_costo(
                            _cf_nuevo,
                            None,
                            data.get("unit_cost_ars"),
                            momento_del_costo_del_alta(stock_inicial, data.get("acquired_at")),
                        )
                        or _cf_nuevo
                    )
                    _nuevo_producto = Product(tenant_id=tenant.tenant_id, **data)
                    session.add(_nuevo_producto)
            except ProductIdentityConflictError as conflict:
                # ``_identity_conflict_from_db`` y no ``_duplicate_identity_conflict``:
                # cuando barcode y sku pertenecen a productos DISTINTOS hay que
                # devolver los dos ids. Colapsarlos en uno elegido por prioridad de
                # índice es adivinar, justo en el caso donde el usuario tiene que
                # decidir cuál corresponde.
                raise _identity_conflict_from_db(
                    conflict, barcode=data.get("barcode"), sku=data.get("sku")
                ) from conflict
            await session.flush()
            if stock_inicial > 0:
                await registrar_apertura_http(
                    session,
                    tenant.tenant_id,
                    _nuevo_producto.id,
                    stock_inicial,
                    costo=data.get("unit_cost_ars"),
                    envio=costo_envio,
                    fecha=data["acquired_at"],
                    confirmado_por=user.user_id,
                    source_upload_id=_upload_id,
                )
            await _ledger_producto_creado(
                session, tenant.tenant_id, _upload_id, _nuevo_producto
            )
            label = "producto"
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.errors(),
        ) from exc

    record.status = UNCLASSIFIED_STATUS_IMPORTED
    record.resolved_at = datetime.now(UTC)
    await session.flush()
    trigger_score_recalculation_after_commit(
        session, str(tenant.tenant_id), "unclassified_reclassified"
    )
    return MessageResponse(message=f"Registro importado como {label}.")


@router.post(
    "/{record_id}/resolve-purchase",
    dependencies=[Depends(require_active_subscription)],
    response_model=MessageResponse,
    summary="Resolver una compra de Otros diciendo qué artículos la componen",
)
async def resolve_purchase(
    record_id: UUID,
    body: ResolvePurchaseRequest,
    tenant: Tenant = Depends(get_current_tenant),
    # F3-T3 review: auth ANTES que el guard 423 — si no, un VIEWER con el tenant
    # lockeado recibiría 423 en vez de 403.
    user: User = Depends(require_role("OWNER", "ADMIN")),
    _maintenance_guard: None = Depends(ensure_tenant_not_under_maintenance),
    session: AsyncSession = Depends(get_db_session),
) -> MessageResponse:
    """B·5b: una compra de mercadería = artículos. Cualquier producto activo del
    negocio (no sólo los candidatos que sugirió el import), con envío e IVA por la
    regla única (`compra_service.registrar_compra`).

    Idempotente por el registro: se toma con `FOR UPDATE` y tiene que estar
    PENDING; dos sesiones a la vez dan una compra y un 409. La compra lleva la
    procedencia de la fila (`source_upload_id` + `source_row_ref`), así que
    borrar el archivo la revierte entera (gasto, stock y costo).
    """
    record = await _get_pending_record(
        session, tenant.tenant_id, record_id, for_update=True
    )
    if record.suggested_entity not in ("expense", None):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "NOT_A_PURCHASE"},
        )
    _clave_identidad = str(record.row_data.get(IDENTITY_REF_KEY) or "")
    if (
        _clave_identidad
        and not body.aplicar_pese_al_conflicto
        and await identidad_tomada(session, tenant.tenant_id, _clave_identidad) is not None
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "IMPORT_IDENTITY_TAKEN",
                "message": (
                    "Esta fila tiene el mismo comprobante que una operación ya "
                    "cargada. Si la registrás igual vas a tener las dos."
                ),
            },
        )

    if body.lines:
        lineas = [
            LineaDeCompra(product_id=ln.product_id, cantidad=ln.quantity, importe=ln.amount)
            for ln in body.lines
        ]
    elif body.target_product_id and body.amount is not None and body.quantity:
        if body.unit_cost is not None and abs(
            body.unit_cost * body.quantity - body.amount
        ) > Decimal("0.01") * body.quantity:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "code": "AMOUNT_MISMATCH",
                    "message": "El monto no coincide con el costo unitario por la cantidad.",
                },
            )
        lineas = [
            LineaDeCompra(
                product_id=body.target_product_id,
                cantidad=body.quantity,
                importe=body.amount,
            )
        ]
    else:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "PURCHASE_NEEDS_ITEMS", "message": MOTIVO_COMPRA_SIN_ARTICULOS},
        )

    _upload_id = record.uploaded_file_id
    try:
        await compra_service.registrar_compra(
            session,
            tenant.tenant_id,
            user.user_id,
            lineas=lineas,
            fecha=body.transaction_date,
            medio_de_pago=body.payment_method,
            envio=(
                Envio(monto=body.shipping.amount, modo=cast(ModoEnvio, body.shipping.mode))
                if body.shipping
                else None
            ),
            iva=(
                Iva(tratamiento=cast(TratamientoIva, body.tax.treatment), alicuota=body.tax.rate)
                if body.tax
                else None
            ),
            supplier_name=body.supplier_name,
            document_ref=body.document_ref,
            source_event_id=f"others:{record_id}",
            source_upload_id=_upload_id,
            source_row_ref=unclassified_row_ref(record.id) if _upload_id else None,
            triggered_by="ui:others_resolve_purchase",
        )
    except ComprobanteInvalidoError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_PURCHASE", "message": str(exc)},
        ) from exc
    record.status = UNCLASSIFIED_STATUS_IMPORTED
    record.resolved_at = datetime.now(UTC)
    await session.flush()
    trigger_score_recalculation_after_commit(
        session, str(tenant.tenant_id), "unclassified_purchase_resolved"
    )
    return MessageResponse(message="Compra registrada con sus artículos.")


@router.post(
    "/bulk-import",
    dependencies=[Depends(require_active_subscription)],
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
        trigger_score_recalculation_after_commit(
        session, str(tenant.tenant_id), "unclassified_bulk_import"
    )
    return BulkImportResponse(**counts)


@router.post(
    "/{record_id}/dismiss",
    response_model=MessageResponse,
    summary="Descartar un registro de Otros",
)
async def dismiss_record(
    record_id: UUID,
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(require_role("OWNER", "ADMIN")),
    session: AsyncSession = Depends(get_db_session),
) -> MessageResponse:
    record = await _get_pending_record(
        session, tenant.tenant_id, record_id, for_update=True
    )
    ahora = datetime.now(UTC)
    record.status = UNCLASSIFIED_STATUS_DISMISSED
    record.resolved_at = ahora
    # B·5a: el descarte de a uno también deja rastro (el de grupo ya lo dejaba).
    session.add(
        DecisionAuditLog(
            tenant_id=tenant.tenant_id,
            decision_type="OTHERS_RECORD_DISMISSED",
            decision_data={
                "record_id": str(record.id),
                "uploaded_file_id": (
                    str(record.uploaded_file_id) if record.uploaded_file_id else None
                ),
                "motivo": record.context_label,
            },
            triggered_by="ui:others_dismiss",
            actor_user_id=user.user_id,
            created_at=ahora,
        )
    )
    await session.flush()
    return MessageResponse(message="Registro descartado.")
