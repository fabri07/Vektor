"""Customer (clientes) CRUD endpoints."""

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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import (
    get_current_tenant,
    require_modify_access,
    require_owner_stepup,
    require_role,
)
from app.application.agents.shared.schemas import LLMCall
from app.application.services.customer_extraction_service import (
    extract_customer,
    parse_customer_records,
)
from app.application.services.customer_import_service import (
    apply_import,
    build_import_preview,
)
from app.application.services.file_parsing import (
    MAX_FILE_SIZE_BYTES,
    SPREADSHEET_MIMES,
    detect_supported_mime,
    sanitize_filename,
)
from app.application.services.idempotency import claim_idempotency_key
from app.observability.logger import get_logger
from app.persistence.db.session import get_db_session
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.customer import Customer
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.persistence.repositories.customer_repository import CustomerRepository
from app.persistence.repositories.transaction_repository import SaleRepository
from app.schemas.common import MessageResponse
from app.schemas.customer import (
    CreateCustomerRequest,
    CustomerBalanceResponse,
    CustomerExtractionResponse,
    CustomerImportConfirmRequest,
    CustomerImportConfirmResponse,
    CustomerImportPreviewItem,
    CustomerImportPreviewResponse,
    CustomerImportRow,
    CustomerResponse,
    UpdateCustomerRequest,
)

logger = get_logger(__name__)

router = APIRouter()


def _customer_snapshot(customer: Customer) -> dict[str, object]:
    return {
        "id": str(customer.id),
        "name": customer.name,
        "customer_type": customer.customer_type,
        "last_name": customer.last_name,
        "doc_type": customer.doc_type,
        "dni": customer.dni,
        "cuit": customer.cuit,
        "iva_condition": customer.iva_condition,
        "email": customer.email,
        "phone": customer.phone,
        "telegram_username": customer.telegram_username,
        "address": customer.address,
        "locality": customer.locality,
        "province": customer.province,
        "postal_code": customer.postal_code,
        "birthday": customer.birthday.isoformat() if customer.birthday else None,
        "notes": customer.notes,
        "custom_fields": customer.custom_fields,
        "deactivated_at": (
            customer.deactivated_at.isoformat() if customer.deactivated_at else None
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
                "record_type": "customer",
                "record_id": before["id"],
                "before": before,
                "after": after,
                "source": "ui",
            },
            triggered_by="ui:data_records",
            actor_user_id=user_id,
            context={"endpoint": "customers"},
            created_at=datetime.now(UTC),
        )
    )


@router.get("", response_model=list[CustomerResponse], summary="List customers")
async def list_customers(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    include_sentinel: bool = Query(
        default=False,
        description="Incluir el cliente centinela 'Local' (ventas sin cliente) si existe.",
    ),
    include_inactive: bool = Query(
        default=False,
        description="Incluir clientes dados de baja (se muestran en rojo en la UI).",
    ),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> list[Customer]:
    repo = CustomerRepository(session)
    return await repo.list_by_tenant(
        tenant.tenant_id,
        limit=limit,
        offset=offset,
        include_sentinel=include_sentinel,
        include_inactive=include_inactive,
    )


@router.post(
    "",
    response_model=CustomerResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a customer",
)
async def create_customer(
    body: CreateCustomerRequest,
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(require_role("OWNER", "ADMIN")),
    session: AsyncSession = Depends(get_db_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Customer:
    # Idempotencia opcional: si llega la key, reclamarla ANTES de crear. Comparte
    # transacción con la creación (commit único en get_db_session): si la creación
    # falla el claim se revierte y un reintento futuro puede entrar.
    if idempotency_key is not None and not await claim_idempotency_key(
        session, tenant.tenant_id, idempotency_key, "IDEMPOTENT_POST_CUSTOMER"
    ):
        raise HTTPException(status_code=409, detail={"code": "DUPLICATE_IDEMPOTENT"})
    # Obligatoriedad backend para alta manual de cliente REAL (identidad + documento
    # + celular). El sentinela "Local" y los imports controlados NO pasan por acá
    # (construyen el Customer directo), así que quedan exentos.
    missing = body.missing_required_fields()
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "CUSTOMER_INCOMPLETE", "missing": missing},
        )
    # No permitir crear un cliente marcado como sentinela desde la API pública.
    if (body.custom_fields or {}).get("_sentinel") in ("true", True):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No se puede crear un cliente sentinela manualmente.",
        )
    repo = CustomerRepository(session)
    customer = Customer(tenant_id=tenant.tenant_id, **body.model_dump())
    saved = await repo.save(customer)
    _audit_data_change(
        session,
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        decision_type="DATA_RECORD_CREATED",
        before=_customer_snapshot(saved),
        after=_customer_snapshot(saved),
    )
    return saved


async def _persist_customer_upload(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    user_id: UUID,
    content: bytes,
    filename: str,
    content_type: str,
    purpose: str,
) -> UUID | None:
    """Guarda el archivo de cliente (S3 + UploadedFile) para trazabilidad.

    Fail-soft: si S3 o la persistencia fallan, devuelve None y la extracción/import
    sigue (no dependen de esto). Espejo de ``_persist_remito_upload``.
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
            purpose=purpose,
            status="uploaded",
            processing_status=PROCESSING_STATUS_DONE,
        )
        session.add(record)
        await session.flush()
        return record.id
    except Exception as exc:  # noqa: BLE001 — trazabilidad best-effort
        logger.warning("customer_upload.persist_failed", error=str(exc))
        return None


async def _read_upload_or_413(file: UploadFile, default_name: str) -> tuple[bytes, str]:
    """Lee el archivo rechazando >16 MB ANTES de materializarlo (Content-Length)."""
    too_large = HTTPException(
        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        detail="El archivo supera el tamaño máximo de 16 MB.",
    )
    if file.size is not None and file.size > MAX_FILE_SIZE_BYTES:
        raise too_large
    content = await file.read()
    if len(content) > MAX_FILE_SIZE_BYTES:  # fallback si no vino Content-Length
        raise too_large
    return content, sanitize_filename(file.filename or default_name)


@router.post(
    "/extract",
    response_model=CustomerExtractionResponse,
    summary="Extract a single customer card from an uploaded file (foto/PDF/planilla)",
)
async def extract_customer_card(
    file: UploadFile = File(...),
    user_hint: str | None = Query(default=None, max_length=500),
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(require_role("OWNER", "ADMIN")),
    session: AsyncSession = Depends(get_db_session),
) -> CustomerExtractionResponse:
    """Lee la ficha de UN cliente y SUGIERE los campos para prellenar el formulario.

    NO persiste el cliente: el alta la confirma el usuario por ``POST /customers``.
    Foto/PDF se leen con IA (transcripción, sin cálculo); planillas se parsean
    determinísticamente (se toma la primera fila). El archivo se guarda best-effort.
    """
    content, filename = await _read_upload_or_413(file, "cliente")

    extraction, usage = await extract_customer(
        content,
        filename,
        content_type=file.content_type,
        user_hint=user_hint,
    )

    if usage is not None:
        llm_call = LLMCall(
            source="customer_extraction",
            model="claude-sonnet-4-6",
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
        )
        logger.info(
            "customer_card.extracted",
            tenant_id=str(tenant.tenant_id),
            confidence=extraction.confidence,
            source=llm_call.source,
            model=llm_call.model,
            input_tokens=llm_call.input_tokens,
            output_tokens=llm_call.output_tokens,
        )

    source_upload_id = await _persist_customer_upload(
        session,
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        content=content,
        filename=filename,
        content_type=file.content_type or "application/octet-stream",
        purpose="customer_card",
    )

    fields = extraction.fields
    return CustomerExtractionResponse(
        customer_type=fields.get("customer_type"),
        name=fields.get("name"),
        last_name=fields.get("last_name"),
        doc_type=fields.get("doc_type"),
        dni=fields.get("dni"),
        cuit=fields.get("cuit"),
        iva_condition=fields.get("iva_condition"),
        email=fields.get("email"),
        phone=fields.get("phone"),
        address=fields.get("address"),
        locality=fields.get("locality"),
        province=fields.get("province"),
        postal_code=fields.get("postal_code"),
        birthday=fields.get("birthday"),
        confidence=extraction.confidence,
        warnings=extraction.warnings,
        source_upload_id=source_upload_id,
    )


@router.post(
    "/import/preview",
    response_model=CustomerImportPreviewResponse,
    summary="Preview a bulk customer import (no persiste)",
)
async def import_customers_preview(
    file: UploadFile = File(...),
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(require_role("OWNER", "ADMIN")),
    session: AsyncSession = Depends(get_db_session),
) -> CustomerImportPreviewResponse:
    """Parsea una planilla de clientes y clasifica cada fila (a crear / a actualizar /
    inválida / duplicada) matcheando por documento contra los existentes. NO persiste."""
    content, filename = await _read_upload_or_413(file, "clientes")
    try:
        mime = detect_supported_mime(content, filename)
    except ValueError as exc:
        return CustomerImportPreviewResponse(
            items=[], to_create=0, to_update=0, invalid=0, duplicates=0,
            warnings=[str(exc)],
        )
    if mime not in SPREADSHEET_MIMES:
        return CustomerImportPreviewResponse(
            items=[], to_create=0, to_update=0, invalid=0, duplicates=0,
            warnings=[
                "El import masivo acepta planillas (XLSX/CSV). Para una foto/PDF usá "
                "'Leer ficha'."
            ],
        )

    records, parse_warnings = parse_customer_records(content, filename, mime)
    repo = CustomerRepository(session)
    existing = await repo.list_for_dedup(tenant.tenant_id)
    preview = build_import_preview(records, existing, parse_warnings=parse_warnings)

    source_upload_id = await _persist_customer_upload(
        session,
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        content=content,
        filename=filename,
        content_type=file.content_type or "application/octet-stream",
        purpose="customer_import",
    )

    return CustomerImportPreviewResponse(
        items=[
            CustomerImportPreviewItem(
                row_index=item.row_index,
                status=item.status,
                customer=CustomerImportRow(**item.fields),
                existing_id=item.existing_id,
                existing_name=item.existing_name,
                issues=item.issues,
            )
            for item in preview.items
        ],
        to_create=preview.to_create,
        to_update=preview.to_update,
        invalid=preview.invalid,
        duplicates=preview.duplicates,
        needs_review=preview.needs_review,
        warnings=preview.warnings,
        source_upload_id=source_upload_id,
    )


@router.post(
    "/import/confirm",
    response_model=CustomerImportConfirmResponse,
    summary="Confirm a bulk customer import (upsert idempotente)",
)
async def import_customers_confirm(
    body: CustomerImportConfirmRequest,
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(require_modify_access),
    session: AsyncSession = Depends(get_db_session),
) -> CustomerImportConfirmResponse:
    """Aplica el import: por cada fila, upsert idempotente por documento (crea o
    actualiza, no duplica). Las filas inválidas se saltean. El sentinela nunca se crea."""
    records = [row.model_dump(exclude_none=True) for row in body.rows]
    repo = CustomerRepository(session)
    result = await apply_import(repo, tenant.tenant_id, records)

    session.add(
        DecisionAuditLog(
            tenant_id=tenant.tenant_id,
            decision_type="DATA_IMPORT_CUSTOMERS",
            decision_data={
                "record_type": "customer",
                "created": len(result.created_ids),
                "updated": len(result.updated_ids),
                "skipped": result.skipped,
                "created_ids": [str(i) for i in result.created_ids],
                "updated_ids": [str(i) for i in result.updated_ids],
                "source": "ui:import",
            },
            triggered_by="ui:customer_import",
            actor_user_id=user.user_id,
            context={"endpoint": "customers/import/confirm"},
            created_at=datetime.now(UTC),
        )
    )

    return CustomerImportConfirmResponse(
        created=len(result.created_ids),
        updated=len(result.updated_ids),
        skipped=result.skipped,
        created_ids=result.created_ids,
        updated_ids=result.updated_ids,
    )


@router.get("/{customer_id}", response_model=CustomerResponse, summary="Get customer by ID")
async def get_customer(
    customer_id: UUID,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> Customer:
    repo = CustomerRepository(session)
    # include_deactivated: el detalle de un cliente dado de baja se puede abrir
    # (historial read-only + opción de reactivar).
    customer = await repo.get_by_id(customer_id, tenant.tenant_id, include_deactivated=True)
    if not customer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found.")
    return customer


@router.patch("/{customer_id}", response_model=CustomerResponse, summary="Update a customer")
async def update_customer(
    customer_id: UUID,
    body: UpdateCustomerRequest,
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(require_modify_access),
    session: AsyncSession = Depends(get_db_session),
) -> Customer:
    repo = CustomerRepository(session)
    customer = await repo.get_by_id(customer_id, tenant.tenant_id)
    if not customer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found.")
    # El sentinela "Local" no se edita (nombre/documentos fijos, agrupador del sistema).
    if customer.is_sentinel:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El cliente 'Local' no se puede editar.",
        )
    before = _customer_snapshot(customer)
    # exclude_unset: aplica solo los campos enviados; deja intactos los no enviados.
    updates = body.model_dump(exclude_unset=True)
    # No permitir convertir un cliente normal en sentinela vía custom_fields.
    if "custom_fields" in updates and (updates["custom_fields"] or {}).get(
        "_sentinel"
    ) in ("true", True):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No se puede convertir un cliente en sentinela.",
        )
    for field, value in updates.items():
        setattr(customer, field, value)
    saved = await repo.save(customer)
    _audit_data_change(
        session,
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        decision_type="DATA_RECORD_UPDATED",
        before=before,
        after=_customer_snapshot(saved),
    )
    return saved


@router.delete(
    "/{customer_id}", response_model=MessageResponse, summary="Soft-delete a customer"
)
async def delete_customer(
    customer_id: UUID,
    force: bool = Query(
        default=False,
        description="Forzar la baja aunque el cliente tenga historial (solo OWNER).",
    ),
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(require_modify_access),
    session: AsyncSession = Depends(get_db_session),
) -> MessageResponse:
    repo = CustomerRepository(session)
    customer = await repo.get_by_id(customer_id, tenant.tenant_id)
    if not customer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found.")
    # El sentinela "Local" no se borra: agrupa ventas sin cliente.
    if customer.is_sentinel:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El cliente 'Local' no se puede eliminar.",
        )
    # Protección de historial: si tiene ventas, solo el OWNER puede forzar la baja.
    sales_count = await repo.count_sales(customer_id, tenant.tenant_id)
    if sales_count > 0:
        if not force:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="HAS_HISTORY",
            )
        if user.role_code != "OWNER":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Solo el dueño de la cuenta puede dar de baja un cliente con historial.",
            )
    before = _customer_snapshot(customer)
    await repo.soft_delete(customer)
    _audit_data_change(
        session,
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        decision_type="DATA_RECORD_VOIDED",
        before=before,
        after=_customer_snapshot(customer),
    )
    return MessageResponse(message="Customer deactivated.")


@router.post(
    "/{customer_id}/reactivate",
    response_model=CustomerResponse,
    summary="Reactivate a soft-deleted customer (OWNER only)",
)
async def reactivate_customer(
    customer_id: UUID,
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(require_owner_stepup),
    session: AsyncSession = Depends(get_db_session),
) -> Customer:
    repo = CustomerRepository(session)
    customer = await repo.get_by_id(customer_id, tenant.tenant_id, include_deactivated=True)
    if not customer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found.")
    before = _customer_snapshot(customer)
    try:
        saved = await repo.reactivate(customer)
    except IntegrityError as exc:
        # Colisión con el índice único parcial (DNI/CUIT) de un cliente ya activo.
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Ya existe un cliente activo con el mismo DNI/CUIT.",
        ) from exc
    _audit_data_change(
        session,
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        decision_type="DATA_RECORD_UPDATED",
        before=before,
        after=_customer_snapshot(saved),
    )
    return saved


@router.get(
    "/{customer_id}/balance",
    response_model=CustomerBalanceResponse,
    summary="Saldo neto del cliente (fiado - cobros)",
)
async def get_customer_balance(
    customer_id: UUID,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> CustomerBalanceResponse:
    """Devuelve el saldo neto del cliente: fiado acumulado menos cobros registrados.

    ``total_account``: suma de ventas con payment_method='account' (fiado).
    ``total_paid``: suma de entradas con payment_method='inflow' (cobros).
    ``balance``: total_account - total_paid.
    ``over_limit``: True si credit_limit está configurado y balance lo supera.

    tenant_id viene del JWT — nunca del path/body.
    404 si el cliente no existe o pertenece a otro tenant.
    """
    customer_repo = CustomerRepository(session)
    customer = await customer_repo.get_by_id(
        customer_id, tenant.tenant_id, include_deactivated=True
    )
    if not customer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found.")

    sale_repo = SaleRepository(session)
    balance_data = await sale_repo.get_balance_by_customer(tenant.tenant_id, customer_id)

    credit_limit = customer.credit_limit
    over_limit = (
        credit_limit is not None and balance_data["balance"] > float(credit_limit)
    )

    return CustomerBalanceResponse(
        customer_id=customer_id,
        total_account=balance_data["total_account"],
        total_paid=balance_data["total_paid"],
        balance=balance_data["balance"],
        credit_limit=credit_limit,
        over_limit=over_limit,
    )
