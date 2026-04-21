"""Tests for chat-oriented file uploads."""

from __future__ import annotations

import unittest.mock
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.file import (
    PROCESSING_STATUS_DONE,
    PROCESSING_STATUS_PENDING,
    UploadedFile,
)


@pytest.fixture
def mock_s3_upload():
    with unittest.mock.patch(
        "app.api.v1.files.S3Client.upload",
        new_callable=unittest.mock.AsyncMock,
        return_value="tenants/test-tenant/fake-file",
    ) as mock:
        yield mock


@pytest.mark.asyncio
class TestFilesUploadEndpoint:
    @pytest.mark.parametrize(
        ("filename", "fixture_name", "expected_file_type", "expected_source_format"),
        [
            ("ventas.csv", "csv_bytes", "spreadsheet", "csv"),
            ("notas.txt", "txt_bytes", "text", "txt"),
            ("resumen.docx", "docx_bytes", "text", "docx"),
            ("slides.pptx", "pptx_bytes", "text", "pptx"),
            ("reporte.pdf", "pdf_bytes", "text", "pdf"),
            ("ticket.png", "png_bytes", "image", "png"),
            ("ticket.jpg", "jpg_bytes", "image", "jpg"),
        ],
    )
    async def test_chat_upload_parses_and_persists_context(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        request: pytest.FixtureRequest,
        mock_s3_upload: unittest.mock.AsyncMock,
        filename: str,
        fixture_name: str,
        expected_file_type: str,
        expected_source_format: str,
    ) -> None:
        content = request.getfixturevalue(fixture_name)

        response = await client.post(
            "/api/v1/files/upload",
            headers=auth_headers,
            params={"purpose": "chat"},
            files={"file": (filename, content, "application/octet-stream")},
        )

        assert response.status_code == 201
        data = response.json()
        file_id = uuid.UUID(data["id"])

        result = await db_session.execute(select(UploadedFile).where(UploadedFile.id == file_id))
        record = result.scalar_one()
        assert record.processing_status == PROCESSING_STATUS_DONE
        assert isinstance(record.parsed_summary_json, dict)
        assert record.parsed_summary_json["file_type"] == expected_file_type
        assert record.parsed_summary_json["source_format"] == expected_source_format
        mock_s3_upload.assert_called()

    async def test_general_upload_keeps_pending_processing_status(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        csv_bytes: bytes,
        mock_s3_upload: unittest.mock.AsyncMock,
    ) -> None:
        response = await client.post(
            "/api/v1/files/upload",
            headers=auth_headers,
            files={"file": ("ventas.csv", csv_bytes, "application/octet-stream")},
        )

        assert response.status_code == 201
        data = response.json()
        file_id = uuid.UUID(data["id"])

        result = await db_session.execute(select(UploadedFile).where(UploadedFile.id == file_id))
        record = result.scalar_one()
        assert record.processing_status == PROCESSING_STATUS_PENDING
        assert record.parsed_summary_json is None

    async def test_upload_rejects_unsupported_type(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        response = await client.post(
            "/api/v1/files/upload",
            headers=auth_headers,
            params={"purpose": "chat"},
            files={"file": ("malware.exe", b"MZfake", "application/octet-stream")},
        )

        assert response.status_code == 415
