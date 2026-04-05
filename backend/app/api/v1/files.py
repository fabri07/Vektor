"""File upload/download endpoints."""

import re
from uuid import UUID

import filetype
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_tenant, get_current_user
from app.integrations.s3 import S3Client
from app.persistence.db.session import get_db_session
from app.persistence.models.file import UploadedFile
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User

router = APIRouter()

ALLOWED_CONTENT_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "text/csv",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB

_SAFE_FILENAME_RE = re.compile(r"[^a-zA-Z0-9.\-_]")


def _sanitize_filename(filename: str) -> str:
    """Remove path traversal and unsafe characters from a filename."""
    filename = filename.replace("\\", "/").split("/")[-1]
    filename = _SAFE_FILENAME_RE.sub("_", filename)
    return filename or "upload"


class UploadedFileResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    original_filename: str
    content_type: str
    size_bytes: int
    purpose: str
    status: str


@router.post(
    "/upload",
    response_model=UploadedFileResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a file (PDF, image, CSV, XLSX)",
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

    # Validate via magic bytes (not just Content-Type header)
    kind = filetype.guess(content[:2048])
    detected_mime = kind.mime if kind else (file.content_type or "")
    if detected_mime not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"File type '{detected_mime}' is not supported.",
        )

    filename = _sanitize_filename(file.filename or "upload")

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
    )
    session.add(record)
    await session.flush()
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
