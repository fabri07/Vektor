"""File upload/download endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_tenant, get_current_user
from app.application.services.file_parsing import (
    MAX_FILE_SIZE_BYTES,
    detect_supported_mime,
    parse_uploaded_content,
    sanitize_filename,
)
from app.application.services.data_intent_extractor import DataIntentExtractor
from app.integrations.s3 import S3Client
from app.persistence.db.session import get_db_session
from app.persistence.models.file import (
    PROCESSING_STATUS_DONE,
    PROCESSING_STATUS_PENDING,
    UploadedFile,
)
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User

router = APIRouter()

class UploadedFileResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    original_filename: str
    content_type: str
    size_bytes: int
    purpose: str
    status: str
    data_intent_detected: bool = False
    suggested_action: str | None = None


@router.post(
    "/upload",
    response_model=UploadedFileResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a file for chat or storage",
)
async def upload_file(
    file: UploadFile = File(...),
    purpose: str = Query(default="general", max_length=50),
    tenant: Tenant = Depends(get_current_tenant),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> UploadedFile:
    content = await file.read()
    if len(content) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="File exceeds maximum size of 10 MB.",
        )

    filename = sanitize_filename(file.filename or "upload")
    try:
        detected_mime = detect_supported_mime(content, filename)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=str(exc),
        ) from exc

    parsed_summary = None
    data_intent_detected = False
    suggested_action = None
    processing_status = None
    if purpose == "chat":
        try:
            parsed_summary = parse_uploaded_content(content, detected_mime, filename)
            pre_check = DataIntentExtractor().check_file_summary(parsed_summary)
            data_intent_detected = pre_check.has_data_intent
            if pre_check.has_data_intent:
                rows = parsed_summary.get("rows_processed", 0)
                suggested_action = (
                    f"¿Querés cargar estos {rows} registros de {pre_check.intent_type}?"
                )
            processing_status = PROCESSING_STATUS_DONE
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"No se pudo procesar el archivo para el chat: {exc}",
            ) from exc

    s3 = S3Client()
    s3_key = await s3.upload(
        content=content,
        filename=filename,
        content_type=detected_mime,
        tenant_id=str(tenant.tenant_id),
    )

    record = UploadedFile(
        tenant_id=tenant.tenant_id,
        uploaded_by=current_user.user_id,
        original_filename=filename,
        s3_key=s3_key,
        content_type=detected_mime,
        size_bytes=len(content),
        purpose=purpose,
        status="uploaded",
        processing_status=processing_status or PROCESSING_STATUS_PENDING,
        parsed_summary_json=parsed_summary,
    )
    session.add(record)
    await session.flush()
    record.data_intent_detected = data_intent_detected
    record.suggested_action = suggested_action
    return record


@router.get("", response_model=list[UploadedFileResponse], summary="List uploaded files")
async def list_files(
    purpose: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> list[UploadedFile]:
    q = select(UploadedFile).where(UploadedFile.tenant_id == tenant.tenant_id)
    if purpose:
        q = q.where(UploadedFile.purpose == purpose)
    q = q.order_by(UploadedFile.created_at.desc()).limit(limit)
    result = await session.execute(q)
    return list(result.scalars().all())


@router.get(
    "/{file_id}/url",
    summary="Get a pre-signed download URL for a file",
)
async def get_download_url(
    file_id: UUID,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, str]:
    result = await session.execute(
        select(UploadedFile).where(
            UploadedFile.id == file_id, UploadedFile.tenant_id == tenant.tenant_id
        )
    )
    record = result.scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found.")

    s3 = S3Client()
    url = await s3.generate_presigned_url(record.s3_key)
    return {"url": url, "expires_in": "3600"}
