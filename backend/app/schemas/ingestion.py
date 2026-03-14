"""Pydantic schemas for the ingestion pipeline endpoints."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class UploadResponse(BaseModel):
    file_id: UUID
    status: str  # always "PROCESSING" immediately after upload


class FileStatusItem(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    original_filename: str
    content_type: str
    size_bytes: int
    purpose: str
    processing_status: str
    created_at: datetime


class FilePreviewResponse(BaseModel):
    model_config = {"from_attributes": True}

    file_id: UUID
    processing_status: str
    parsed_summary_json: dict[str, Any] | None


class ConfirmIngestionRequest(BaseModel):
    confirmed_fields: dict[str, Any] = Field(
        description=(
            "Which data categories to import from the parsed file. "
            "Keys: 'ventas', 'gastos', 'productos'. Values: bool."
        )
    )


class ConfirmIngestionResponse(BaseModel):
    file_id: UUID
    status: str
    message: str
