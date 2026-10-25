"""Tests for chat-oriented file uploads."""

from __future__ import annotations

import unittest.mock
import uuid
from collections.abc import Iterator

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.persistence.models.file import (
    PROCESSING_STATUS_DONE,
    PROCESSING_STATUS_PENDING,
    UploadedFile,
)
from app.persistence.models.tenant import Tenant
from app.tests.api.v1.test_permisos_de_importacion import _headers_de


@pytest.fixture
def mock_s3_upload():
    with unittest.mock.patch(
        "app.api.v1.files.S3Client.upload",
        new_callable=unittest.mock.AsyncMock,
        return_value="tenants/test-tenant/fake-file",
    ) as mock:
        yield mock


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


# ── R-6: subir fuera del chat exige OWNER/ADMIN ──────────────────────────────
#
# Revisión profunda 2026-10-24: con un `purpose` distinto de "chat" (el default
# es "general") cualquier rol subía hasta 16 MB a S3 y dejaba un UploadedFile
# PENDING. El frontend sólo sube con `purpose=chat`, que sigue abierto a todos.


@pytest_asyncio.fixture(params=["VIEWER", "ANALYST"])
async def sin_permiso(
    request: pytest.FixtureRequest, db_session: AsyncSession, sample_tenant: Tenant
) -> dict[str, str]:
    return await _headers_de(db_session, sample_tenant, str(request.param))


async def _archivos(db: AsyncSession, tenant: Tenant) -> int:
    return int(
        (
            await db.execute(
                select(func.count(UploadedFile.id)).where(
                    UploadedFile.tenant_id == tenant.tenant_id
                )
            )
        ).scalar_one()
    )


@pytest.fixture
def espia_de_lectura() -> Iterator[unittest.mock.MagicMock]:
    """Registra cada `await file.read()` sin cambiar lo que devuelve."""
    original = StarletteUploadFile.read
    with unittest.mock.patch.object(
        StarletteUploadFile, "read", autospec=True, side_effect=original
    ) as espia:
        yield espia


class TestSubidaSinChatPorRol:
    @pytest.mark.parametrize("params", [{"purpose": "general"}, {}], ids=["general", "omitido"])
    async def test_sin_permiso_no_sube_ni_registra(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        sin_permiso: dict[str, str],
        csv_bytes: bytes,
        mock_s3_upload: unittest.mock.AsyncMock,
        espia_de_lectura: unittest.mock.MagicMock,
        params: dict[str, str],
    ) -> None:
        response = await client.post(
            "/api/v1/files/upload",
            headers=sin_permiso,
            params=params,
            files={"file": ("ventas.csv", csv_bytes, "application/octet-stream")},
        )

        assert response.status_code == 403
        espia_de_lectura.assert_not_called()
        mock_s3_upload.assert_not_called()
        assert await _archivos(db_session, sample_tenant) == 0

    async def test_viewer_sigue_subiendo_al_chat(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        viewer_headers: dict[str, str],
        csv_bytes: bytes,
        mock_s3_upload: unittest.mock.AsyncMock,
    ) -> None:
        response = await client.post(
            "/api/v1/files/upload",
            headers=viewer_headers,
            params={"purpose": "chat"},
            files={"file": ("ventas.csv", csv_bytes, "application/octet-stream")},
        )

        assert response.status_code == 201
        mock_s3_upload.assert_called_once()
        assert await _archivos(db_session, sample_tenant) == 1

    @pytest.mark.parametrize("rol", ["OWNER", "ADMIN"])
    async def test_owner_y_admin_suben_fuera_del_chat(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        csv_bytes: bytes,
        mock_s3_upload: unittest.mock.AsyncMock,
        espia_de_lectura: unittest.mock.MagicMock,
        rol: str,
    ) -> None:
        headers = await _headers_de(db_session, sample_tenant, rol)

        response = await client.post(
            "/api/v1/files/upload",
            headers=headers,
            params={"purpose": "general"},
            files={"file": ("ventas.csv", csv_bytes, "application/octet-stream")},
        )

        assert response.status_code == 201
        # El espía ve la lectura: su `assert_not_called` del caso rechazado no es vacuo.
        espia_de_lectura.assert_called()
        mock_s3_upload.assert_called_once()
        record = (
            await db_session.execute(
                select(UploadedFile).where(UploadedFile.id == uuid.UUID(response.json()["id"]))
            )
        ).scalar_one()
        assert record.purpose == "general"
        assert record.processing_status == PROCESSING_STATUS_PENDING
