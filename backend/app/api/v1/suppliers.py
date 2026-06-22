"""Supplier (proveedores) CRUD endpoints."""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_tenant, require_role
from app.application.agents.shared.schemas import LLMCall
from app.application.services.file_parsing import (
    MAX_FILE_SIZE_BYTES,
    sanitize_filename,
)
from app.application.services.idempotency import claim_idempotency_key
from app.application.services.ingestion_import_service import (
    ReceiptLine,
    import_receipt,
)
from app.application.services.remito_extraction_service import extract_remito
from app.observability.logger import get_logger
from app.persistence.db.session import get_db_session
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.supplier import Supplier
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.persistence.repositories.inventory_repository import InventoryRepository
from app.persistence.repositories.supplier_repository import SupplierRepository
from app.schemas.common import MessageResponse
from app.schemas.supplier import (
    CreateReceiptRequest,
    CreateSupplierRequest,
    ReceiptExtractionLine,
    ReceiptExtractionResponse,
    ReceiptResponse,
    SupplierProductPurchaseResponse,
    SupplierResponse,
    UpdateSupplierRequest,
)

logger = get_logger(__name__)

router = APIRouter()


def _supplier_snapshot(supplier: Supplier) -> dict[str, object]:
    return {
        "id": str(supplier.id),
        "name": supplier.name,
        "last_name": supplier.last_name,
        "cuil": supplier.cuil,
        "payment_method": supplier.payment_method,
        "email": supplier.email,
        "phone": supplier.phone,
        "notes": supplier.notes,
        "custom_fields": supplier.custom_fields,
        "deactivated_at": (
            supplier.deactivated_at.isoformat() if supplier.deactivated_at else None
        ),
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
                "record_type": "supplier",
                "record_id": before["id"],
                "before": before,
                "after": after,
                "source": "ui",
            },
            triggered_by="ui:data_records",
            actor_user_id=user_id,
            context={"endpoint": "suppliers"},
            created_at=datetime.now(UTC),
        )
    )


@router.get("", response_model=list[SupplierResponse], summary="List suppliers")
async def list_suppliers(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> list[Supplier]:
    repo = SupplierRepository(session)
    return await repo.list_by_tenant(tenant.tenant_id, limit=limit, offset=offset)


@router.post(
    "",
    response_model=SupplierResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a supplier",
)
async def create_supplier(
    body: CreateSupplierRequest,
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(require_role("OWNER", "ADMIN")),
    session: AsyncSession = Depends(get_db_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Supplier:
    # Idempotencia opcional: si llega la key, reclamarla ANTES de crear. Comparte
    # transacción con la creación (commit único en get_db_session): si la creación
    # falla el claim se revierte y un reintento futuro puede entrar.
    if idempotency_key is not None and not await claim_idempotency_key(
        session, tenant.tenant_id, idempotency_key, "IDEMPOTENT_POST_SUPPLIER"
    ):
        raise HTTPException(status_code=409, detail={"code": "DUPLICATE_IDEMPOTENT"})
    repo = SupplierRepository(session)
    supplier = Supplier(tenant_id=tenant.tenant_id, **body.model_dump())
    saved = await repo.save(supplier)
    _audit_data_change(
        session,
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        decision_type="DATA_RECORD_CREATED",
        before=_supplier_snapshot(saved),
        after=_supplier_snapshot(saved),
    )
    return saved


@router.get("/{supplier_id}", response_model=SupplierResponse, summary="Get supplier by ID")
async def get_supplier(
    supplier_id: UUID,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> Supplier:
    repo = SupplierRepository(session)
    supplier = await repo.get_by_id(supplier_id, tenant.tenant_id)
    if not supplier:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Supplier not found.")
    return supplier


@router.get(
    "/{supplier_id}/products",
    response_model=list[SupplierProductPurchaseResponse],
    summary="List products purchased from a supplier",
)
async def list_supplier_products(
    supplier_id: UUID,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> list[SupplierProductPurchaseResponse]:
    """FASE 3: productos comprados a un proveedor (última compra / cantidad / precio
    unitario), desde ``inventory_movements``. Aislado por tenant del JWT: 404 si el
    proveedor no pertenece al tenant.
    """
    repo = SupplierRepository(session)
    supplier = await repo.get_by_id(supplier_id, tenant.tenant_id)
    if not supplier:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Supplier not found.")
    inv = InventoryRepository(session)
    purchases = await inv.products_purchased_from_supplier(tenant.tenant_id, supplier_id)
    return [SupplierProductPurchaseResponse.model_validate(p) for p in purchases]


@router.post(
    "/{supplier_id}/receipts",
    response_model=ReceiptResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Import a supplier receipt (remito)",
)
async def create_receipt(
    supplier_id: UUID,
    body: CreateReceiptRequest,
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(require_role("OWNER", "ADMIN")),
    session: AsyncSession = Depends(get_db_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ReceiptResponse:
    """FASE 4: registra un remito del proveedor del path: por línea, producto
    (alta/stock) + gasto COGS; el envío → gasto OPEX ``LOGISTICS``. Transacción
    única, fail-closed. Idempotente vía ``Idempotency-Key`` (replay → 409).
    """
    repo = SupplierRepository(session)
    supplier = await repo.get_by_id(supplier_id, tenant.tenant_id)
    if not supplier:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Supplier not found.")
    # Un remito identifica al proveedor: no se puede cargar contra el sentinela
    # "No identificado" (agrupa compras SIN proveedor conocido). Completá/creá un
    # proveedor real primero.
    if supplier.is_sentinel:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No se puede cargar un remito contra el proveedor 'No identificado'.",
        )
    # Idempotencia: reclamar la key ANTES de escribir (comparte transacción).
    if idempotency_key is not None and not await claim_idempotency_key(
        session, tenant.tenant_id, idempotency_key, "IDEMPOTENT_POST_RECEIPT"
    ):
        raise HTTPException(status_code=409, detail={"code": "DUPLICATE_IDEMPOTENT"})

    lines = [
        ReceiptLine(
            product_name=ln.product_name,
            sku=ln.sku,
            qty=int(ln.qty),
            unit_price=ln.unit_price,
        )
        for ln in body.lines
    ]
    summary = await import_receipt(
        session,
        tenant.tenant_id,
        supplier_id,
        lines,
        shipping_cost=body.shipping_cost,
        transaction_date=body.transaction_date,
        source_upload_id=body.source_upload_id,
    )
    _audit_data_change(
        session,
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        decision_type="RECEIPT_IMPORTED",
        before={"id": str(supplier_id)},
        after={"id": str(supplier_id), **summary},
    )
    return ReceiptResponse(
        lines=summary["lines"],
        products_created=[UUID(pid) for pid in summary["products_created"]],
        expense_ids=[UUID(eid) for eid in summary["expense_ids"]],
        shipping_expense_id=(
            UUID(summary["shipping_expense_id"])
            if summary["shipping_expense_id"]
            else None
        ),
        total_cogs_ars=summary["total_cogs_ars"],
    )


async def _persist_remito_upload(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    user_id: UUID,
    content: bytes,
    filename: str,
    content_type: str,
) -> UUID | None:
    """Guarda el archivo del remito (S3 + UploadedFile) para trazabilidad.

    Fail-soft: si S3 o la persistencia fallan, devuelve None y la extracción sigue
    (el alta final no depende de esto). No bloquea la lectura del remito.
    """
    from app.integrations.s3 import S3Client  # noqa: PLC0415
    from app.persistence.models.file import (  # noqa: PLC0415
        PROCESSING_STATUS_DONE,
        UploadedFile,
    )

    try:
        s3_key = await S3Client().upload(
            content=content,
            filename=filename,
            content_type=content_type,
            tenant_id=str(tenant_id),
        )
        record = UploadedFile(
            tenant_id=tenant_id,
            uploaded_by=user_id,
            original_filename=filename,
            s3_key=s3_key,
            content_type=content_type,
            size_bytes=len(content),
            purpose="receipt",
            status="uploaded",
            processing_status=PROCESSING_STATUS_DONE,
        )
        session.add(record)
        await session.flush()
        return record.id
    except Exception as exc:  # noqa: BLE001 — trazabilidad best-effort
        logger.warning("supplier_receipt.upload_persist_failed", error=str(exc))
        return None


@router.post(
    "/{supplier_id}/receipts/extract",
    response_model=ReceiptExtractionResponse,
    summary="Extract receipt lines from an uploaded file (foto/PDF/planilla)",
)
async def extract_receipt(
    supplier_id: UUID,
    file: UploadFile = File(...),
    user_hint: str | None = Query(default=None, max_length=500),
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(require_role("OWNER", "ADMIN")),
    session: AsyncSession = Depends(get_db_session),
) -> ReceiptExtractionResponse:
    """Lee el archivo de un remito y SUGIERE las líneas (producto/cantidad/precio).

    NO persiste transacciones: el alta la confirma el usuario por
    ``POST /suppliers/{id}/receipts``. Foto/PDF se leen con IA (transcripción, sin
    cálculo); planillas XLSX/CSV se parsean determinísticamente. El archivo se guarda
    (best-effort) para trazabilidad → ``source_upload_id``.
    """
    repo = SupplierRepository(session)
    supplier = await repo.get_by_id(supplier_id, tenant.tenant_id)
    if not supplier:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Supplier not found.")
    # Un remito identifica al proveedor: no contra el sentinela "No identificado".
    if supplier.is_sentinel:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No se puede leer un remito contra el proveedor 'No identificado'.",
        )

    # Rechazar por Content-Length ANTES de materializar el archivo en memoria
    # (evita cargar un upload gigante en RAM solo para descartarlo después).
    too_large = HTTPException(
        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        detail="El archivo supera el tamaño máximo de 16 MB.",
    )
    if file.size is not None and file.size > MAX_FILE_SIZE_BYTES:
        raise too_large
    content = await file.read()
    if len(content) > MAX_FILE_SIZE_BYTES:  # fallback si no vino Content-Length
        raise too_large
    filename = sanitize_filename(file.filename or "remito")

    extraction, usage = await extract_remito(
        content,
        filename,
        content_type=file.content_type,
        user_hint=user_hint,
    )

    # Trazabilidad de uso de IA (cuando hubo llamada al modelo). Se registra el
    # LLMCall en el log estructurado (mismo patrón source/model/tokens del resto).
    if usage is not None:
        llm_call = LLMCall(
            source="remito_extraction",
            model="claude-sonnet-4-6",
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
        )
        logger.info(
            "supplier_receipt.extracted",
            tenant_id=str(tenant.tenant_id),
            lines=len(extraction.lines),
            confidence=extraction.confidence,
            source=llm_call.source,
            model=llm_call.model,
            input_tokens=llm_call.input_tokens,
            output_tokens=llm_call.output_tokens,
        )

    # Guardar el archivo (best-effort) para trazabilidad import → archivo origen.
    source_upload_id = await _persist_remito_upload(
        session,
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        content=content,
        filename=filename,
        content_type=file.content_type or "application/octet-stream",
    )

    return ReceiptExtractionResponse(
        lines=[
            ReceiptExtractionLine(
                product_name=ln.product_name,
                sku=ln.sku,
                qty=ln.qty,
                unit_price=ln.unit_price,
            )
            for ln in extraction.lines
        ],
        shipping_cost=extraction.shipping_cost,
        currency=extraction.currency,
        confidence=extraction.confidence,
        warnings=extraction.warnings,
        source_upload_id=source_upload_id,
    )


@router.patch("/{supplier_id}", response_model=SupplierResponse, summary="Update a supplier")
async def update_supplier(
    supplier_id: UUID,
    body: UpdateSupplierRequest,
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(require_role("OWNER", "ADMIN")),
    session: AsyncSession = Depends(get_db_session),
) -> Supplier:
    repo = SupplierRepository(session)
    supplier = await repo.get_by_id(supplier_id, tenant.tenant_id)
    if not supplier:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Supplier not found.")
    before = _supplier_snapshot(supplier)
    # exclude_unset: aplica solo los campos enviados; deja intactos los no enviados.
    updates = body.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(supplier, field, value)
    saved = await repo.save(supplier)
    _audit_data_change(
        session,
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        decision_type="DATA_RECORD_UPDATED",
        before=before,
        after=_supplier_snapshot(saved),
    )
    return saved


@router.delete(
    "/{supplier_id}", response_model=MessageResponse, summary="Soft-delete a supplier"
)
async def delete_supplier(
    supplier_id: UUID,
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(require_role("OWNER", "ADMIN")),
    session: AsyncSession = Depends(get_db_session),
) -> MessageResponse:
    repo = SupplierRepository(session)
    supplier = await repo.get_by_id(supplier_id, tenant.tenant_id)
    if not supplier:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Supplier not found.")
    before = _supplier_snapshot(supplier)
    await repo.soft_delete(supplier)
    _audit_data_change(
        session,
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        decision_type="DATA_RECORD_VOIDED",
        before=before,
        after=_supplier_snapshot(supplier),
    )
    return MessageResponse(message="Supplier deactivated.")
