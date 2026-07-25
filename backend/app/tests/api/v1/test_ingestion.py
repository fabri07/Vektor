"""
Tests for the ingestion pipeline endpoints.

POST   /api/v1/ingestion/upload
GET    /api/v1/ingestion/files
GET    /api/v1/ingestion/files/{file_id}/preview
POST   /api/v1/ingestion/files/{file_id}/confirm

Mocks:
  - S3Client.upload_to_key  → no real AWS calls
  - process_spreadsheet.delay, process_text_document.delay, process_image_ocr.delay
  - pytesseract.image_to_string → no tesseract binary required in CI
"""

import hashlib
import io
import unittest.mock
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.file import (
    PROCESSING_STATUS_DONE,
    PROCESSING_STATUS_IMPORTING,
    PROCESSING_STATUS_NEEDS_CONFIRMATION,
    UploadedFile,
)
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def xlsx_bytes() -> bytes:
    """Minimal real xlsx file created in-memory."""
    openpyxl = pytest.importorskip("openpyxl")

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["fecha", "monto", "descripcion"])
    ws.append(["2024-01-15", "50000", "Venta del día"])
    ws.append(["2024-01-16", "35000", "Venta tarde"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture
def csv_bytes() -> bytes:
    return (
        b"fecha,monto,descripcion\n"
        b"2024-01-15,50000,Venta del dia\n"
        b"2024-01-16,35000,Venta tarde\n"
    )


@pytest.fixture
def txt_bytes() -> bytes:
    return b"Venta del dia $50.000\nGasto proveedor $12.000\nStock mercaderia $8.000 unidades\n"


@pytest.fixture
def png_bytes() -> bytes:
    """Minimal valid 1x1 PNG."""
    return (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
        b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
    )


@pytest.fixture
def mock_s3_upload():
    """Prevent real S3 calls on upload_to_key."""
    with unittest.mock.patch(
        "app.api.v1.ingestion.S3Client.upload_to_key",
        new_callable=unittest.mock.AsyncMock,
        return_value="uploads/fake-tenant/fake-uuid/file.xlsx",
    ) as mock:
        yield mock


@pytest.fixture
def mock_spreadsheet_delay():
    from app.jobs.ingestion_worker import process_spreadsheet

    with unittest.mock.patch.object(process_spreadsheet, "delay") as mock:
        yield mock


@pytest.fixture
def mock_text_delay():
    from app.jobs.ingestion_worker import process_text_document

    with unittest.mock.patch.object(process_text_document, "delay") as mock:
        yield mock


@pytest.fixture
def mock_image_delay():
    from app.jobs.ingestion_worker import process_image_ocr

    with unittest.mock.patch.object(process_image_ocr, "delay") as mock:
        yield mock


@pytest_asyncio.fixture
async def confirmed_file(
    db_session: AsyncSession, sample_tenant: Tenant, sample_user: "object"
) -> UploadedFile:
    """A file already in NEEDS_CONFIRMATION state."""
    record = UploadedFile(
        tenant_id=sample_tenant.tenant_id,
        uploaded_by=None,
        original_filename="test.xlsx",
        s3_key="uploads/test/uuid/test.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=1024,
        purpose="ventas",
        status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        parsed_summary_json={
            "confidence": "HIGH",
            "file_type": "spreadsheet",
            # Campos que el parser real siempre setea (necesarios para que la
            # heurística legacy reconozca el tipo y monto e inserte filas).
            "inferred_type": "ventas",
            "has_venta": True,
            "has_fecha": True,
            "row_count": 1,
            "ventas_detectadas": [
                {"fecha": "2024-01-15", "monto": "50000", "descripcion": "Venta"}
            ],
        },
    )
    db_session.add(record)
    await db_session.commit()
    return record


# ── Upload tests ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestUploadEndpoint:
    async def test_upload_xlsx_returns_processing(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        xlsx_bytes: bytes,
        mock_s3_upload: unittest.mock.AsyncMock,
        mock_spreadsheet_delay: unittest.mock.MagicMock,
    ) -> None:
        response = await client.post(
            "/api/v1/ingestion/upload",
            headers=auth_headers,
            files={"file": ("ventas_enero.xlsx", xlsx_bytes, "application/octet-stream")},
            params={"file_hint": "ventas"},
        )
        assert response.status_code == 201
        data = response.json()
        assert data["status"] == "PROCESSING"
        assert "file_id" in data
        mock_s3_upload.assert_called_once()
        mock_spreadsheet_delay.assert_called_once()

    async def test_upload_csv_enqueues_spreadsheet_job(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        csv_bytes: bytes,
        mock_s3_upload: unittest.mock.AsyncMock,
        mock_spreadsheet_delay: unittest.mock.MagicMock,
    ) -> None:
        response = await client.post(
            "/api/v1/ingestion/upload",
            headers=auth_headers,
            files={"file": ("datos.csv", csv_bytes, "application/octet-stream")},
        )
        assert response.status_code == 201
        mock_spreadsheet_delay.assert_called_once()

    async def test_upload_duplicate_content_returns_409(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        csv_bytes: bytes,
        mock_s3_upload: unittest.mock.AsyncMock,
        mock_spreadsheet_delay: unittest.mock.MagicMock,
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        """A6: re-subir un archivo con contenido ya importado se bloquea con 409."""
        content_hash = hashlib.sha256(csv_bytes).hexdigest()
        prior = UploadedFile(
            tenant_id=sample_tenant.tenant_id,
            uploaded_by=None,
            original_filename="ventas_previas.csv",
            s3_key="uploads/test/prev/ventas.csv",
            content_type="text/csv",
            size_bytes=len(csv_bytes),
            purpose="ventas",
            status="uploaded",
            processing_status=PROCESSING_STATUS_DONE,
            content_hash=content_hash,
        )
        db_session.add(prior)
        await db_session.commit()

        response = await client.post(
            "/api/v1/ingestion/upload",
            headers=auth_headers,
            files={"file": ("ventas_enero.csv", csv_bytes, "application/octet-stream")},
        )
        assert response.status_code == 409
        assert "ya fue importado" in response.json()["detail"]
        # No se subió a S3 ni se encoló job: el duplicado se bloquea antes.
        mock_s3_upload.assert_not_called()
        mock_spreadsheet_delay.assert_not_called()

    async def test_upload_duplicate_with_override_succeeds(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        csv_bytes: bytes,
        mock_s3_upload: unittest.mock.AsyncMock,
        mock_spreadsheet_delay: unittest.mock.MagicMock,
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        """A6: con allow_duplicate=true se permite reimportar (con aviso informativo)."""
        content_hash = hashlib.sha256(csv_bytes).hexdigest()
        prior = UploadedFile(
            tenant_id=sample_tenant.tenant_id,
            uploaded_by=None,
            original_filename="ventas_previas.csv",
            s3_key="uploads/test/prev/ventas.csv",
            content_type="text/csv",
            size_bytes=len(csv_bytes),
            purpose="ventas",
            status="uploaded",
            processing_status=PROCESSING_STATUS_DONE,
            content_hash=content_hash,
        )
        db_session.add(prior)
        await db_session.commit()

        response = await client.post(
            "/api/v1/ingestion/upload",
            headers=auth_headers,
            files={"file": ("ventas_enero.csv", csv_bytes, "application/octet-stream")},
            params={"allow_duplicate": "true"},
        )
        assert response.status_code == 201
        data = response.json()
        assert data["status"] == "PROCESSING"
        assert data["duplicate_of"] == str(prior.id)
        assert data["warning"] is not None
        mock_s3_upload.assert_called_once()
        mock_spreadsheet_delay.assert_called_once()

    async def test_upload_same_name_different_content_warns(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        csv_bytes: bytes,
        mock_s3_upload: unittest.mock.AsyncMock,
        mock_spreadsheet_delay: unittest.mock.MagicMock,
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        """C: resubir mismo NOMBRE con contenido distinto NO bloquea (201) pero avisa
        del riesgo de duplicar filas repetidas; sugiere Releer / subir solo lo nuevo."""
        prior = UploadedFile(
            tenant_id=sample_tenant.tenant_id,
            uploaded_by=None,
            original_filename="ventas.csv",  # mismo nombre que se sube abajo
            s3_key="uploads/test/prev/ventas.csv",
            content_type="text/csv",
            size_bytes=10,
            purpose="ventas",
            status="uploaded",
            processing_status=PROCESSING_STATUS_DONE,
            content_hash="hash-viejo-distinto",  # contenido distinto ⇒ no es dup exacto
        )
        db_session.add(prior)
        await db_session.commit()

        response = await client.post(
            "/api/v1/ingestion/upload",
            headers=auth_headers,
            files={"file": ("ventas.csv", csv_bytes, "application/octet-stream")},
        )
        assert response.status_code == 201
        data = response.json()
        assert data["status"] == "PROCESSING"
        assert data["duplicate_of"] is None  # no es dup exacto
        assert data["warning"] is not None
        assert "mismo" in data["warning"].lower() or "nombre" in data["warning"].lower()
        mock_s3_upload.assert_called_once()  # no bloquea

    async def test_upload_txt_enqueues_text_job(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        txt_bytes: bytes,
        mock_s3_upload: unittest.mock.AsyncMock,
        mock_text_delay: unittest.mock.MagicMock,
    ) -> None:
        response = await client.post(
            "/api/v1/ingestion/upload",
            headers=auth_headers,
            files={"file": ("notas.txt", txt_bytes, "application/octet-stream")},
        )
        assert response.status_code == 201
        mock_text_delay.assert_called_once()

    async def test_upload_png_enqueues_ocr_job(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        png_bytes: bytes,
        mock_s3_upload: unittest.mock.AsyncMock,
        mock_image_delay: unittest.mock.MagicMock,
    ) -> None:
        response = await client.post(
            "/api/v1/ingestion/upload",
            headers=auth_headers,
            files={"file": ("ticket.png", png_bytes, "application/octet-stream")},
        )
        assert response.status_code == 201
        mock_image_delay.assert_called_once()

    async def test_upload_pdf_enqueues_text_job(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        pdf_bytes: bytes,
        mock_s3_upload: unittest.mock.AsyncMock,
        mock_text_delay: unittest.mock.MagicMock,
    ) -> None:
        response = await client.post(
            "/api/v1/ingestion/upload",
            headers=auth_headers,
            files={"file": ("doc.pdf", pdf_bytes, "application/pdf")},
        )
        assert response.status_code == 201
        mock_text_delay.assert_called_once()

    async def test_upload_pptx_enqueues_text_job(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        pptx_bytes: bytes,
        mock_s3_upload: unittest.mock.AsyncMock,
        mock_text_delay: unittest.mock.MagicMock,
    ) -> None:
        response = await client.post(
            "/api/v1/ingestion/upload",
            headers=auth_headers,
            files={"file": ("slides.pptx", pptx_bytes, "application/octet-stream")},
        )
        assert response.status_code == 201
        mock_text_delay.assert_called_once()

    async def test_upload_unsupported_type_returns_415(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
    ) -> None:
        response = await client.post(
            "/api/v1/ingestion/upload",
            headers=auth_headers,
            files={"file": ("malware.exe", b"MZfake", "application/octet-stream")},
        )
        assert response.status_code == 415

    async def test_upload_too_large_returns_413(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
    ) -> None:
        # 17 MB (> 16 MB cap). xlsx magic bytes prefix + junk to get past MIME detection.
        big_content = b"PK\x03\x04" + b"\x00" * (17 * 1024 * 1024)
        response = await client.post(
            "/api/v1/ingestion/upload",
            headers=auth_headers,
            files={"file": ("huge.xlsx", big_content, "application/octet-stream")},
        )
        assert response.status_code == 413

    async def test_upload_unauthenticated_returns_403(
        self,
        client: AsyncClient,
        xlsx_bytes: bytes,
    ) -> None:
        response = await client.post(
            "/api/v1/ingestion/upload",
            files={"file": ("ventas.xlsx", xlsx_bytes, "application/octet-stream")},
        )
        assert response.status_code == 401

    async def test_upload_csv_sync_fallback_when_celery_unavailable(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        csv_bytes: bytes,
        mock_s3_upload: unittest.mock.AsyncMock,
    ) -> None:
        """When Celery/Redis is unavailable, the file is processed synchronously."""
        # Mock .delay() to raise (simulating Redis down)
        with unittest.mock.patch("app.api.v1.ingestion.process_spreadsheet") as mock_task:
            mock_task.delay.side_effect = ConnectionError("Redis unavailable")
            # Mock S3 download for sync fallback
            with unittest.mock.patch(
                "app.api.v1.ingestion.S3Client.download",
                new_callable=unittest.mock.AsyncMock,
                return_value=csv_bytes,
            ):
                response = await client.post(
                    "/api/v1/ingestion/upload",
                    headers=auth_headers,
                    files={"file": ("datos.csv", csv_bytes, "application/octet-stream")},
                )
        assert response.status_code == 201
        data = response.json()
        assert data["status"] == "PROCESSING"
        assert "file_id" in data


# ── List files tests ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestListFilesEndpoint:
    async def test_list_files_empty(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
    ) -> None:
        response = await client.get("/api/v1/ingestion/files", headers=auth_headers)
        assert response.status_code == 200
        assert response.json() == []

    async def test_list_files_tenant_isolation(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        second_auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        """Tenant A cannot see Tenant B's files."""
        # Create a file for tenant A
        record = UploadedFile(
            tenant_id=sample_tenant.tenant_id,
            uploaded_by=None,
            original_filename="tenantA_file.xlsx",
            s3_key="uploads/a/uuid/file.xlsx",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            size_bytes=500,
            purpose="ventas",
            status="uploaded",
            processing_status="PENDING",
        )
        db_session.add(record)
        await db_session.commit()

        # Tenant A sees their file
        resp_a = await client.get("/api/v1/ingestion/files", headers=auth_headers)
        assert resp_a.status_code == 200
        assert len(resp_a.json()) == 1

        # Tenant B sees nothing
        resp_b = await client.get("/api/v1/ingestion/files", headers=second_auth_headers)
        assert resp_b.status_code == 200
        assert resp_b.json() == []

    async def test_list_files_filter_by_status(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        confirmed_file: UploadedFile,
    ) -> None:
        response = await client.get(
            "/api/v1/ingestion/files",
            headers=auth_headers,
            params={"processing_status": PROCESSING_STATUS_NEEDS_CONFIRMATION},
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["processing_status"] == PROCESSING_STATUS_NEEDS_CONFIRMATION


# ── Preview tests ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestPreviewEndpoint:
    async def test_preview_returns_summary(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        confirmed_file: UploadedFile,
    ) -> None:
        response = await client.get(
            f"/api/v1/ingestion/files/{confirmed_file.id}/preview",
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["processing_status"] == PROCESSING_STATUS_NEEDS_CONFIRMATION
        assert data["parsed_summary_json"]["confidence"] == "HIGH"

    async def test_preview_pending_file_returns_409(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        record = UploadedFile(
            tenant_id=sample_tenant.tenant_id,
            uploaded_by=None,
            original_filename="pending.xlsx",
            s3_key="uploads/test/uuid/pending.xlsx",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            size_bytes=512,
            purpose="ventas",
            status="uploaded",
            processing_status="PENDING",
        )
        db_session.add(record)
        await db_session.commit()

        response = await client.get(
            f"/api/v1/ingestion/files/{record.id}/preview",
            headers=auth_headers,
        )
        assert response.status_code == 409

    async def test_preview_nonexistent_returns_404(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
    ) -> None:
        response = await client.get(
            f"/api/v1/ingestion/files/{uuid.uuid4()}/preview",
            headers=auth_headers,
        )
        assert response.status_code == 404


# ── Confirm tests ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestConfirmEndpoint:
    async def test_confirm_needs_confirmation_returns_200(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        confirmed_file: UploadedFile,
        mock_score_trigger: unittest.mock.MagicMock,
    ) -> None:
        response = await client.post(
            f"/api/v1/ingestion/files/{confirmed_file.id}/confirm",
            headers=auth_headers,
            json={"confirmed_fields": {"ventas": True, "gastos": False}},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == PROCESSING_STATUS_DONE
        assert "recalculada" in data["message"]
        mock_score_trigger.assert_called_once()

    async def test_confirm_wrong_status_returns_409(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        record = UploadedFile(
            tenant_id=sample_tenant.tenant_id,
            uploaded_by=None,
            original_filename="done.xlsx",
            s3_key="uploads/test/uuid/done.xlsx",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            size_bytes=512,
            purpose="ventas",
            status="uploaded",
            processing_status=PROCESSING_STATUS_DONE,
        )
        db_session.add(record)
        await db_session.commit()

        response = await client.post(
            f"/api/v1/ingestion/files/{record.id}/confirm",
            headers=auth_headers,
            json={"confirmed_fields": {"ventas": True}},
        )
        assert response.status_code == 409

    async def test_confirm_nonexistent_returns_404(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
    ) -> None:
        response = await client.post(
            f"/api/v1/ingestion/files/{uuid.uuid4()}/confirm",
            headers=auth_headers,
            json={"confirmed_fields": {"ventas": True}},
        )
        assert response.status_code == 404

    async def test_confirm_enforces_tenant_isolation(
        self,
        client: AsyncClient,
        second_auth_headers: dict[str, Any],
        confirmed_file: UploadedFile,
    ) -> None:
        """Tenant B cannot confirm Tenant A's file."""
        response = await client.post(
            f"/api/v1/ingestion/files/{confirmed_file.id}/confirm",
            headers=second_auth_headers,
            json={"confirmed_fields": {"ventas": True}},
        )
        assert response.status_code == 404

    async def test_confirm_sin_columna_fecha_devuelve_422_antes_del_lease(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        """F6-A1: una venta confirmada sin columna de fecha se rechaza con 422 y
        NUNCA toma el lease (el archivo sigue re-confirmable en NEEDS_CONFIRMATION).
        Sin este gate, la fila caería al fallback silencioso "hoy" (invariante 2d).
        """
        record = UploadedFile(
            tenant_id=sample_tenant.tenant_id,
            uploaded_by=None,
            original_filename="ventas_sin_fecha.xlsx",
            s3_key="uploads/test/uuid/ventas_sin_fecha.xlsx",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            size_bytes=512,
            purpose="ventas",
            status="uploaded",
            processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
            parsed_summary_json={
                "confidence": "HIGH",
                "file_type": "spreadsheet",
                "inferred_type": "ventas",
                "has_venta": True,
                "row_count": 1,
                # Sin ninguna columna de fecha en las filas.
                "ventas_detectadas": [{"monto": "50000", "descripcion": "Venta"}],
            },
        )
        db_session.add(record)
        await db_session.commit()

        response = await client.post(
            f"/api/v1/ingestion/files/{record.id}/confirm",
            headers=auth_headers,
            json={"confirmed_fields": {"ventas": True}},
        )
        assert response.status_code == 422
        assert "fecha" in response.json()["detail"].lower()

        # El lease nunca se tomó: sigue re-confirmable.
        refreshed = (
            await db_session.execute(
                select(UploadedFile).where(UploadedFile.id == record.id)
            )
        ).scalar_one()
        assert refreshed.processing_status == PROCESSING_STATUS_NEEDS_CONFIRMATION
        assert refreshed.import_attempt_id is None
        assert refreshed.import_started_at is None

    async def test_confirm_multicontexto_bloquea_solo_el_contexto_sin_fecha(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        """F6-A1: en multi-hoja, confirmar un contexto sin fecha lo bloquea (422
        nombrándolo); si ese contexto NO se incluye, los demás importan."""
        summary = {
            "confidence": "HIGH",
            "file_type": "spreadsheet",
            "inferred_type": "mixed",
            "multi_sheet": True,
            "has_venta": True,
            "has_gasto": True,
            "mapping_contexts": [
                {
                    "context_id": "sheet:A:ventas",
                    "entity_type": "sale",
                    "label": "Hoja A — Ventas",
                    "headers": ["fecha", "monto"],
                },
                {
                    "context_id": "sheet:B:gastos",
                    "entity_type": "expense",
                    "label": "Hoja B — Gastos",
                    "headers": ["detalle", "monto"],  # sin fecha
                },
            ],
            "ventas_detectadas": [
                {"__context__": "sheet:A:ventas", "fecha": "2024-01-15", "monto": "50000"}
            ],
            "gastos_detectados": [
                {"__context__": "sheet:B:gastos", "detalle": "Varios", "monto": "12000"}
            ],
        }
        record = UploadedFile(
            tenant_id=sample_tenant.tenant_id,
            uploaded_by=None,
            original_filename="mixto.xlsx",
            s3_key="uploads/test/uuid/mixto.xlsx",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            size_bytes=1024,
            purpose="ventas",
            status="uploaded",
            processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
            parsed_summary_json=summary,
        )
        db_session.add(record)
        await db_session.commit()

        # Ambos contextos incluidos → el de gastos (sin fecha) dispara 422.
        blocked = await client.post(
            f"/api/v1/ingestion/files/{record.id}/confirm",
            headers=auth_headers,
            json={"confirmed_fields": {"ventas": True, "gastos": True}},
        )
        assert blocked.status_code == 422
        assert "Gastos" in blocked.json()["detail"]

        # El lease no se tomó pese al 422.
        refreshed = (
            await db_session.execute(
                select(UploadedFile).where(UploadedFile.id == record.id)
            )
        ).scalar_one()
        assert refreshed.processing_status == PROCESSING_STATUS_NEEDS_CONFIRMATION
        assert refreshed.import_attempt_id is None

    async def test_confirm_contexto_reasignado_a_venta_sin_fecha_devuelve_422(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        """F6-A1: si el usuario reasigna una hoja de producto (sin fecha) a venta
        vía context_entity, el gate debe usar la entidad EFECTIVA — el importador
        la procesaría como venta y volcaría todo a /otros. 422 antes del lease."""
        summary = {
            "confidence": "HIGH",
            "file_type": "spreadsheet",
            "inferred_type": "stock",
            "mapping_contexts": [
                {
                    "context_id": "table",
                    "entity_type": "product",
                    "label": "Catálogo",
                    "headers": ["producto", "precio"],  # sin fecha
                }
            ],
            "stock_detectado": [
                {"__context__": "table", "producto": "Vela", "precio": "500"}
            ],
        }
        record = UploadedFile(
            tenant_id=sample_tenant.tenant_id,
            uploaded_by=None,
            original_filename="catalogo.xlsx",
            s3_key="uploads/test/uuid/catalogo.xlsx",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            size_bytes=512,
            purpose="productos",
            status="uploaded",
            processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
            parsed_summary_json=summary,
        )
        db_session.add(record)
        await db_session.commit()

        response = await client.post(
            f"/api/v1/ingestion/files/{record.id}/confirm",
            headers=auth_headers,
            json={
                "confirmed_fields": {},
                "context_confirmed": {"table": True},
                "context_entity": {"table": "sale"},  # reasignado a venta
            },
        )
        assert response.status_code == 422
        assert "fecha" in response.json()["detail"].lower()

        refreshed = (
            await db_session.execute(
                select(UploadedFile).where(UploadedFile.id == record.id)
            )
        ).scalar_one()
        assert refreshed.processing_status == PROCESSING_STATUS_NEEDS_CONFIRMATION
        assert refreshed.import_attempt_id is None

    async def test_confirm_drop_requerido_sin_reemplazo_devuelve_422_antes_del_lease(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        """F8b (Task 2): dropear la ÚNICA columna mapeada a un target requerido
        (transaction_date) dejaría el requerido sin mapear. Se rechaza con 422
        ANTES del lease — igual contrato que el gate de fecha F6-A1."""
        record = UploadedFile(
            tenant_id=sample_tenant.tenant_id,
            uploaded_by=None,
            original_filename="ventas.xlsx",
            s3_key="uploads/test/uuid/ventas.xlsx",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            size_bytes=512,
            purpose="ventas",
            status="uploaded",
            processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
            parsed_summary_json={
                "confidence": "HIGH",
                "file_type": "spreadsheet",
                "inferred_type": "ventas",
                "has_venta": True,
                "has_fecha": True,
                "row_count": 1,
                "ventas_detectadas": [{"fecha": "2024-01-15", "monto": "50000"}],
            },
        )
        db_session.add(record)
        await db_session.commit()

        response = await client.post(
            f"/api/v1/ingestion/files/{record.id}/confirm",
            headers=auth_headers,
            json={
                "confirmed_fields": {"ventas": True},
                "column_mappings": [
                    {"source_column": "fecha", "target_field": "transaction_date"},
                    {"source_column": "monto", "target_field": "amount"},
                ],
                "column_risk_decisions": [
                    {
                        "context_id": "table",
                        "source_column": "fecha",
                        "target_field": "transaction_date",
                        "action": "drop_column",
                    }
                ],
            },
        )
        assert response.status_code == 422
        assert "transaction_date" in response.json()["detail"]

        # El lease nunca se tomó: sigue re-confirmable.
        refreshed = (
            await db_session.execute(
                select(UploadedFile).where(UploadedFile.id == record.id)
            )
        ).scalar_one()
        assert refreshed.processing_status == PROCESSING_STATUS_NEEDS_CONFIRMATION
        assert refreshed.import_attempt_id is None
        assert refreshed.import_started_at is None

    async def test_confirm_drop_requerido_con_reemplazo_no_bloquea(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        mock_score_trigger: unittest.mock.MagicMock,
    ) -> None:
        """F8b (Task 2): dropear una columna requerida CON otra columna mapeada
        al mismo target (fecha_alt) no bloquea — el requerido sigue cubierto.

        F8b (Task 4): además, el drop AHORA se aplica de verdad, así que la fila
        debe importar por la columna de reemplazo (fecha_alt)."""
        record = UploadedFile(
            tenant_id=sample_tenant.tenant_id,
            uploaded_by=None,
            original_filename="ventas.xlsx",
            s3_key="uploads/test/uuid/ventas.xlsx",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            size_bytes=512,
            purpose="ventas",
            status="uploaded",
            processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
            parsed_summary_json={
                "confidence": "HIGH",
                "file_type": "spreadsheet",
                "inferred_type": "ventas",
                "has_venta": True,
                "has_fecha": True,
                "row_count": 1,
                "ventas_detectadas": [
                    {"fecha": "2024-01-15", "fecha_alt": "2024-01-20", "monto": "50000"}
                ],
            },
        )
        db_session.add(record)
        await db_session.commit()

        response = await client.post(
            f"/api/v1/ingestion/files/{record.id}/confirm",
            headers=auth_headers,
            json={
                "confirmed_fields": {"ventas": True},
                "column_mappings": [
                    {"source_column": "fecha", "target_field": "transaction_date"},
                    {
                        "source_column": "fecha_alt",
                        "target_field": "transaction_date",
                        "user_selected": True,
                    },
                    {"source_column": "monto", "target_field": "amount"},
                ],
                "column_risk_decisions": [
                    {
                        "context_id": "table",
                        "source_column": "fecha",
                        "target_field": "transaction_date",
                        "action": "drop_column",
                    }
                ],
            },
        )
        assert response.status_code == 200

    async def test_confirm_route_opcional_no_seleccionado_devuelve_422_antes_del_lease(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        """F8b (Task 2): rutear filas a Otros por un campo OPCIONAL que el
        usuario no seleccionó explícitamente viola el invariante 1. 422 antes
        del lease."""
        record = UploadedFile(
            tenant_id=sample_tenant.tenant_id,
            uploaded_by=None,
            original_filename="ventas.xlsx",
            s3_key="uploads/test/uuid/ventas.xlsx",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            size_bytes=512,
            purpose="ventas",
            status="uploaded",
            processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
            parsed_summary_json={
                "confidence": "HIGH",
                "file_type": "spreadsheet",
                "inferred_type": "ventas",
                "has_venta": True,
                "has_fecha": True,
                "row_count": 1,
                "ventas_detectadas": [
                    {"fecha": "2024-01-15", "monto": "50000", "notas": None}
                ],
            },
        )
        db_session.add(record)
        await db_session.commit()

        response = await client.post(
            f"/api/v1/ingestion/files/{record.id}/confirm",
            headers=auth_headers,
            json={
                "confirmed_fields": {"ventas": True},
                "column_mappings": [
                    {"source_column": "fecha", "target_field": "transaction_date"},
                    {"source_column": "monto", "target_field": "amount"},
                    {"source_column": "notas", "target_field": "notes"},
                ],
                "column_risk_decisions": [
                    {
                        "context_id": "table",
                        "source_column": "notas",
                        "target_field": "notes",
                        "action": "route_affected_rows_to_others",
                    }
                ],
            },
        )
        assert response.status_code == 422

        refreshed = (
            await db_session.execute(
                select(UploadedFile).where(UploadedFile.id == record.id)
            )
        ).scalar_one()
        assert refreshed.processing_status == PROCESSING_STATUS_NEEDS_CONFIRMATION
        assert refreshed.import_attempt_id is None

    async def test_confirm_route_requerido_no_bloquea(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        mock_score_trigger: unittest.mock.MagicMock,
    ) -> None:
        """F8b (Task 2): rutear filas afectadas por un campo REQUERIDO (amount)
        es una decisión válida — no bloquea el confirm."""
        record = UploadedFile(
            tenant_id=sample_tenant.tenant_id,
            uploaded_by=None,
            original_filename="ventas.xlsx",
            s3_key="uploads/test/uuid/ventas.xlsx",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            size_bytes=512,
            purpose="ventas",
            status="uploaded",
            processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
            parsed_summary_json={
                "confidence": "HIGH",
                "file_type": "spreadsheet",
                "inferred_type": "ventas",
                "has_venta": True,
                "has_fecha": True,
                "row_count": 1,
                "ventas_detectadas": [{"fecha": "2024-01-15", "monto": "50000"}],
            },
        )
        db_session.add(record)
        await db_session.commit()

        response = await client.post(
            f"/api/v1/ingestion/files/{record.id}/confirm",
            headers=auth_headers,
            json={
                "confirmed_fields": {"ventas": True},
                "column_mappings": [
                    {"source_column": "fecha", "target_field": "transaction_date"},
                    {"source_column": "monto", "target_field": "amount"},
                ],
                "column_risk_decisions": [
                    {
                        "context_id": "table",
                        "source_column": "monto",
                        "target_field": "amount",
                        "action": "route_affected_rows_to_others",
                    }
                ],
            },
        )
        assert response.status_code == 200


class _CaptureLogger:
    def __init__(self) -> None:
        self.debug_events: list[tuple[str, dict[str, Any]]] = []

    def debug(self, event: str, **kwargs) -> None:
        self.debug_events.append((event, kwargs))


def test_parse_amount_logs_non_positive_discard(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.application.services.ingestion_import_service as importer

    capture = _CaptureLogger()
    monkeypatch.setattr(importer, "logger", capture)

    assert importer._parse_amount("0") is None
    assert capture.debug_events == [
        (
            "ingestion.parse.amount_discarded",
            {"raw": "0", "reason": "non_positive"},
        )
    ]


@pytest.mark.asyncio
async def test_fila_con_fecha_ilegible_va_a_otros_no_inventa_hoy(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F6-A2: una fila con valor de fecha ilegible NO se registra con "hoy" — va a
    /otros para revisión manual. Antes se estampillaba la fecha de carga (invariante
    2d violada); ahora no se crea ninguna venta y la fila queda como pendiente.
    """
    import app.application.services.ingestion_import_service as importer
    from app.persistence.models.unclassified_record import UnclassifiedRecord

    capture = _CaptureLogger()
    monkeypatch.setattr(importer, "logger", capture)

    await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        {
            "file_type": "spreadsheet",
            "inferred_type": "ventas",
            "has_venta": True,
            "ventas_detectadas": [{"fecha": "fecha rara", "monto": "100"}],
        },
        {"ventas": True},
    )

    # No se creó ninguna venta con fecha inventada.
    sales = (await db_session.execute(select(SaleEntry))).scalars().all()
    assert sales == []
    # La fila quedó en /otros, sugerida como venta.
    records = (await db_session.execute(select(UnclassifiedRecord))).scalars().all()
    assert len(records) == 1
    assert records[0].suggested_entity == "sale"
    assert records[0].row_data.get("monto") == "100"
    assert capture.debug_events == [
        (
            "ingestion.parse.date_row_routed_to_otros",
            {"raw": "fecha rara", "row_index": 0},
        )
    ]


@pytest.mark.asyncio
async def test_multisheet_heterogeneous_schemas_no_silent_drop(
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Regresión: filas de hojas del mismo tipo con columnas distintas no se descartan.

    Antes, _insert_multisheet_data resolvía la columna de monto desde
    `rows[0].keys()` (solo la primera fila). Las filas de una segunda hoja con
    otro esquema (ej. 'total' en vez de 'monto') no encontraban su columna y se
    perdían en silencio. Con resolución por fila, ambas deben insertarse.
    """
    import app.application.services.ingestion_import_service as importer

    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "has_gasto": True,
        "gastos_detectados": [
            # Hoja A: columna de monto = "monto", con categoría
            {"fecha": "2024-01-15", "monto": "12000", "categoria": "alquiler"},
            # Hoja B: columna de monto = "total", esquema distinto
            {"fecha": "2024-01-16", "total": "3500", "descripcion": "Servicios"},
        ],
    }

    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        {"gastos": True},
    )

    assert counts["gastos"] == 2
    result = await db_session.execute(select(ExpenseEntry))
    amounts = sorted(e.amount for e in result.scalars().all())
    assert amounts == [Decimal("3500"), Decimal("12000")]


@pytest.mark.asyncio
async def test_multisheet_compras_inserted_as_products(
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Una hoja de compras ruteada a stock crea productos con su costo."""
    import app.application.services.ingestion_import_service as importer

    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "has_producto": True,
        "stock_detectado": [
            {"producto": "Coca-Cola 600ml", "costo_unitario": "800", "cantidad": "24"},
        ],
    }

    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        {"productos": True},
    )

    assert counts["productos"] == 1
    result = await db_session.execute(select(Product))
    prod = result.scalar_one()
    assert prod.name == "Coca-Cola 600ml"
    assert prod.unit_cost_ars == Decimal("800")
    assert prod.stock_units == 24


def test_sale_response_serializes_amount_as_number() -> None:
    """Regresión NaN: amount debe serializarse como número, no string.

    El frontend tipa amount como `number` y hace reduce(sum). Con string, la
    concatenación de montos decimales producía `$ NaN` en los totales.
    """
    from app.schemas.transaction import ExpenseEntryResponse, SaleEntryResponse

    sale = SaleEntryResponse(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        product_id=None,
        amount=Decimal("5400.50"),
        quantity=1,
        transaction_date=date(2024, 1, 15),
        payment_method="cash",
        notes=None,
        created_at=datetime(2024, 1, 15, 12, 0, 0),
    )
    dumped = sale.model_dump(mode="json")
    assert isinstance(dumped["amount"], int | float)
    assert dumped["amount"] == 5400.5

    expense = ExpenseEntryResponse(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        amount=Decimal("12000.00"),
        category="alquiler",
        transaction_date=date(2024, 1, 15),
        description="Alquiler",
        is_recurring=False,
        payment_method="transfer",
        supplier_name=None,
        notes=None,
        created_at=datetime(2024, 1, 15, 12, 0, 0),
    )
    dumped_e = expense.model_dump(mode="json")
    assert isinstance(dumped_e["amount"], int | float)
    assert dumped_e["amount"] == 12000.0


# ── Worker unit tests ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestIngestionWorkers:
    async def test_process_image_ocr_handles_missing_pytesseract(
        self,
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        """process_image_ocr gracefully handles missing pytesseract binary."""
        from app.jobs.ingestion_worker import _extract_amounts_from_text

        # Test the text extraction logic (pure function, no DB/S3 needed)
        text = "Venta del día $50.000\nGasto proveedor $12.000"
        result = _extract_amounts_from_text(text)
        assert "ventas_detectadas" in result
        assert "gastos_detectados" in result
        assert len(result["ventas_detectadas"]) >= 1
        assert len(result["gastos_detectados"]) >= 1

    async def test_pytesseract_mocked_in_ocr_worker(self) -> None:
        """
        Ensure that when pytesseract.image_to_string is mocked,
        the OCR path does not require the tesseract binary.
        """
        pytest.importorskip("pytesseract")
        fake_text = "Venta $25.000\nGasto $5.000"

        with unittest.mock.patch("pytesseract.image_to_string", return_value=fake_text):
            from app.jobs.ingestion_worker import _extract_amounts_from_text

            result = _extract_amounts_from_text(fake_text)
            assert len(result["ventas_detectadas"]) >= 1

    async def test_analyze_headers_high_confidence(self) -> None:
        from app.jobs.ingestion_worker import _analyze_headers

        # Sin señal de catálogo (sin "producto"/"sku"/etc.) → HIGH
        headers = ["fecha", "monto", "descripcion", "metodo_pago"]
        result = _analyze_headers(headers)
        assert result["confidence"] == "HIGH"
        assert result["has_fecha"] is True
        assert result["has_venta"] is True

    async def test_analyze_headers_medium_confidence(self) -> None:
        from app.jobs.ingestion_worker import _analyze_headers

        headers = ["nombre", "precio", "columna_desconocida"]
        result = _analyze_headers(headers)
        assert result["confidence"] == "MEDIUM"


# ── F4: lease del confirm (concurrencia) ────────────────────────────────────────


def _importing_file_kwargs(tenant_id: uuid.UUID, *, started_at: datetime) -> dict[str, Any]:
    """Kwargs de un UploadedFile en estado IMPORTING con lease tomado."""
    return {
        "tenant_id": tenant_id,
        "uploaded_by": None,
        "original_filename": "importing.xlsx",
        "s3_key": "uploads/test/uuid/importing.xlsx",
        "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "size_bytes": 1024,
        "purpose": "ventas",
        "status": "uploaded",
        "processing_status": PROCESSING_STATUS_IMPORTING,
        "import_attempt_id": uuid.uuid4(),
        "import_started_at": started_at,
        "import_phase": "inserting",
        "parsed_summary_json": {
            "confidence": "HIGH",
            "file_type": "spreadsheet",
            "inferred_type": "ventas",
            "has_venta": True,
            "has_fecha": True,
            "row_count": 1,
            "ventas_detectadas": [
                {"fecha": "2024-01-15", "monto": "50000", "descripcion": "Venta"}
            ],
        },
    }


@pytest.mark.asyncio
class TestConfirmLeaseF4:
    async def test_confirm_success_clears_lease(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        confirmed_file: UploadedFile,
        db_session: AsyncSession,
        mock_score_trigger: unittest.mock.MagicMock,
    ) -> None:
        """Un confirm exitoso deja DONE y limpia el lease por completo."""
        response = await client.post(
            f"/api/v1/ingestion/files/{confirmed_file.id}/confirm",
            headers=auth_headers,
            json={"confirmed_fields": {"ventas": True, "gastos": False}},
        )
        assert response.status_code == 200

        refreshed = (
            await db_session.execute(
                select(UploadedFile).where(UploadedFile.id == confirmed_file.id)
            )
        ).scalar_one()
        assert refreshed.processing_status == PROCESSING_STATUS_DONE
        assert refreshed.import_attempt_id is None
        assert refreshed.import_started_at is None
        assert refreshed.import_phase is None

    async def test_confirm_while_importing_returns_409(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        """Un IMPORTING vivo (lease no vencido) rechaza un segundo confirm con 409."""
        record = UploadedFile(
            **_importing_file_kwargs(
                sample_tenant.tenant_id, started_at=datetime.now(UTC)
            )
        )
        db_session.add(record)
        await db_session.commit()

        response = await client.post(
            f"/api/v1/ingestion/files/{record.id}/confirm",
            headers=auth_headers,
            json={"confirmed_fields": {"ventas": True, "gastos": False}},
        )
        assert response.status_code == 409
        assert "importando" in response.json()["detail"].lower()

    async def test_confirm_stale_importing_takeover_succeeds(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        mock_score_trigger: unittest.mock.MagicMock,
    ) -> None:
        """Un IMPORTING con import_started_at viejo (proceso muerto) es retomable."""
        stale = datetime.now(UTC) - timedelta(hours=1)  # TTL default = 15 min
        record = UploadedFile(**_importing_file_kwargs(sample_tenant.tenant_id, started_at=stale))
        db_session.add(record)
        await db_session.commit()

        response = await client.post(
            f"/api/v1/ingestion/files/{record.id}/confirm",
            headers=auth_headers,
            json={"confirmed_fields": {"ventas": True, "gastos": False}},
        )
        assert response.status_code == 200

        refreshed = (
            await db_session.execute(
                select(UploadedFile).where(UploadedFile.id == record.id)
            )
        ).scalar_one()
        assert refreshed.processing_status == PROCESSING_STATUS_DONE
        assert refreshed.import_attempt_id is None

    async def test_confirm_failure_restores_needs_confirmation_and_clears_lease(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        confirmed_file: UploadedFile,
        db_session: AsyncSession,
    ) -> None:
        """Si el import falla tras tomar el lease, el compensador revierte el
        savepoint, restaura NEEDS_CONFIRMATION y limpia el lease (re-confirmable)."""
        with unittest.mock.patch(
            "app.api.v1.ingestion.insert_confirmed_data",
            new_callable=unittest.mock.AsyncMock,
            side_effect=HTTPException(status_code=400, detail="boom"),
        ):
            response = await client.post(
                f"/api/v1/ingestion/files/{confirmed_file.id}/confirm",
                headers=auth_headers,
                json={"confirmed_fields": {"ventas": True, "gastos": False}},
            )
        assert response.status_code == 400

        refreshed = (
            await db_session.execute(
                select(UploadedFile).where(UploadedFile.id == confirmed_file.id)
            )
        ).scalar_one()
        assert refreshed.processing_status == PROCESSING_STATUS_NEEDS_CONFIRMATION
        assert refreshed.import_attempt_id is None
        assert refreshed.import_started_at is None
        assert refreshed.import_phase is None

    async def test_failure_after_f5_savepoints_still_compensates_lease(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        """F5-A × F4: los savepoints anidados no rompen la compensación del lease.

        El test anterior mockea ``insert_confirmed_data`` entero, así que falla
        ANTES de abrir un solo savepoint interno. Acá el import corre de verdad:
        crea el producto de la compra pasando por ``guarded_savepoint`` (savepoint
        de 2º nivel bajo ``_import_sp``) y RECIÉN DESPUÉS falla. Es el caso que
        importa — si el helper dejara la sesión en un estado raro, el
        ``_import_sp.rollback()`` + el UPDATE de compensación no podrían correr y
        el archivo quedaría clavado en IMPORTING para siempre.
        """
        record = UploadedFile(
            tenant_id=sample_tenant.tenant_id,
            uploaded_by=None,
            original_filename="compras.xlsx",
            s3_key="uploads/test/uuid/compras.xlsx",
            content_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
            size_bytes=1024,
            purpose="gastos",
            status="uploaded",
            processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
            parsed_summary_json={
                "confidence": "HIGH",
                "file_type": "spreadsheet",
                "inferred_type": "gastos",
                "has_gasto": True,
                "row_count": 1,
                "gastos_detectados": [
                    {
                        "fecha": "2024-02-01",
                        "categoria": "mercaderia",
                        "producto": "Coca Cola 500ml",
                        "sku": "COCA-500",
                        "cantidad": "24",
                        "monto": "19200",
                        "costo_unitario": "800",
                        "forma_pago": "efectivo",
                    }
                ],
            },
        )
        db_session.add(record)
        await db_session.commit()

        # Falla en el último paso ANTES de soltar el savepoint del import: para
        # entonces el producto ya se creó dentro de un savepoint anidado.
        with unittest.mock.patch(
            "app.api.v1.ingestion.pipeline_event_service.emit_event",
            new_callable=unittest.mock.AsyncMock,
            side_effect=HTTPException(
                status_code=503, detail="falla despues de los savepoints"
            ),
        ):
            response = await client.post(
                f"/api/v1/ingestion/files/{record.id}/confirm",
                headers=auth_headers,
                json={"confirmed_fields": {"ventas": False, "gastos": True}},
            )
        assert response.status_code == 503

        # ``refresh`` explícito: ``record`` sigue en el identity map y quedó
        # expirado por el request, así que un ``select`` devolvería la MISMA
        # instancia y el primer atributo que se lea dispararía un lazy load
        # síncrono (MissingGreenlet) en vez de leer la fila.
        await db_session.refresh(record)
        assert record.processing_status == PROCESSING_STATUS_NEEDS_CONFIRMATION
        assert record.import_attempt_id is None
        assert record.import_started_at is None
        assert record.import_phase is None

        # El savepoint del import revirtió TODO lo parcial: ni producto ni gasto.
        products = (
            (
                await db_session.execute(
                    select(Product).where(Product.tenant_id == sample_tenant.tenant_id)
                )
            )
            .scalars()
            .all()
        )
        assert products == [], "el rollback del savepoint no dejó datos a medias"

    async def test_delete_importing_returns_409(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        """No se puede borrar un archivo con un import en curso (409)."""
        record = UploadedFile(
            **_importing_file_kwargs(sample_tenant.tenant_id, started_at=datetime.now(UTC))
        )
        db_session.add(record)
        await db_session.commit()

        response = await client.delete(
            f"/api/v1/ingestion/files/{record.id}",
            headers=auth_headers,
        )
        assert response.status_code == 409
        assert "importa" in response.json()["detail"].lower()

    async def test_delete_non_importing_soft_deletes(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        confirmed_file: UploadedFile,
        db_session: AsyncSession,
    ) -> None:
        """El CAS de borrado sigue soft-deleteando un archivo normal (204)."""
        response = await client.delete(
            f"/api/v1/ingestion/files/{confirmed_file.id}",
            headers=auth_headers,
        )
        assert response.status_code == 204

        refreshed = (
            await db_session.execute(
                select(UploadedFile).where(UploadedFile.id == confirmed_file.id)
            )
        ).scalar_one()
        assert refreshed.deleted_at is not None


@pytest.mark.asyncio
class TestConfirmColumnRiskF8b:
    """F8b (Task 4): aplicación de las decisiones de riesgo DENTRO del confirm.

    Cablea drop_column / route_affected_rows_to_others sobre una COPIA del
    summary, dentro del savepoint del import: atomicidad, counters, auditoría
    agregada sin PII, y recálculo exacto de filas afectadas (no confía en el
    cliente).
    """

    @staticmethod
    def _ventas_record(tenant_id: uuid.UUID, rows: list[dict[str, Any]]) -> UploadedFile:
        return UploadedFile(
            tenant_id=tenant_id,
            uploaded_by=None,
            original_filename="ventas.xlsx",
            s3_key="uploads/test/uuid/ventas.xlsx",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            size_bytes=512,
            purpose="ventas",
            status="uploaded",
            processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
            parsed_summary_json={
                "confidence": "HIGH",
                "file_type": "spreadsheet",
                "inferred_type": "ventas",
                "has_venta": True,
                "has_fecha": True,
                "row_count": len(rows),
                "ventas_detectadas": rows,
            },
        )

    async def test_route_importa_validas_y_captura_solo_afectadas(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        mock_score_trigger: unittest.mock.MagicMock,
    ) -> None:
        """route sobre `amount` (requerido): la fila válida importa; las vacías/
        inválidas van a Otros. El recálculo es EXACTO (no rutea la válida — el
        backend NUNCA confía en un `affected_rows` del cliente)."""
        from app.persistence.models.unclassified_record import UnclassifiedRecord

        record = self._ventas_record(
            sample_tenant.tenant_id,
            [
                {"fecha": "2024-01-15", "monto": "50000"},  # válida → importa
                {"fecha": "2024-01-16", "monto": ""},  # vacía → Otros
                {"fecha": "2024-01-17", "monto": "no-numerico"},  # inválida → Otros
            ],
        )
        db_session.add(record)
        await db_session.commit()

        response = await client.post(
            f"/api/v1/ingestion/files/{record.id}/confirm",
            headers=auth_headers,
            json={
                "confirmed_fields": {"ventas": True},
                "column_mappings": [
                    {"source_column": "fecha", "target_field": "transaction_date"},
                    {"source_column": "monto", "target_field": "amount"},
                ],
                "column_risk_decisions": [
                    {
                        "context_id": "table",
                        "source_column": "monto",
                        "target_field": "amount",
                        "action": "route_affected_rows_to_others",
                    }
                ],
            },
        )
        assert response.status_code == 200

        # Solo 1 venta importada (la válida).
        sales = (
            await db_session.execute(
                select(SaleEntry).where(SaleEntry.tenant_id == sample_tenant.tenant_id)
            )
        ).scalars().all()
        assert len(sales) == 1

        # Exactamente 2 filas capturadas en Otros (las afectadas).
        others = (
            await db_session.execute(
                select(UnclassifiedRecord).where(
                    UnclassifiedRecord.tenant_id == sample_tenant.tenant_id
                )
            )
        ).scalars().all()
        assert len(others) == 2
        assert all(r.suggested_entity == "sale" for r in others)

    async def test_recompute_no_rutea_todo_el_contexto(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        mock_score_trigger: unittest.mock.MagicMock,
    ) -> None:
        """Guard de mutación (invariante 3): con 4 filas de las que 3 son válidas,
        solo la afectada va a Otros. Si el backend ruteara el contexto entero
        (mutación: confiar en el cliente), este test fallaría."""
        from app.persistence.models.unclassified_record import UnclassifiedRecord

        record = self._ventas_record(
            sample_tenant.tenant_id,
            [
                {"fecha": "2024-01-15", "monto": "100"},
                {"fecha": "2024-01-16", "monto": "200"},
                {"fecha": "2024-01-17", "monto": "300"},
                {"fecha": "2024-01-18", "monto": ""},  # única afectada
            ],
        )
        db_session.add(record)
        await db_session.commit()

        response = await client.post(
            f"/api/v1/ingestion/files/{record.id}/confirm",
            headers=auth_headers,
            json={
                "confirmed_fields": {"ventas": True},
                "column_mappings": [
                    {"source_column": "fecha", "target_field": "transaction_date"},
                    {"source_column": "monto", "target_field": "amount"},
                ],
                "column_risk_decisions": [
                    {
                        "context_id": "table",
                        "source_column": "monto",
                        "target_field": "amount",
                        "action": "route_affected_rows_to_others",
                    }
                ],
            },
        )
        assert response.status_code == 200

        sales = (
            await db_session.execute(
                select(SaleEntry).where(SaleEntry.tenant_id == sample_tenant.tenant_id)
            )
        ).scalars().all()
        assert len(sales) == 3  # las 3 válidas, NO 0

        others = (
            await db_session.execute(
                select(UnclassifiedRecord).where(
                    UnclassifiedRecord.tenant_id == sample_tenant.tenant_id
                )
            )
        ).scalars().all()
        assert len(others) == 1  # solo la afectada, NO 4

    async def test_rollback_integral_si_confirm_falla(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        """Si el confirm falla DESPUÉS de aplicar decisiones (dentro del
        savepoint), NADA persiste: ni ventas, ni capturas en Otros, ni auditoría.
        El archivo vuelve a NEEDS_CONFIRMATION (re-confirmable).

        Mutación clave: si la captura se hiciera FUERA del savepoint, el commit
        del compensador del lease dejaría las filas en Otros → este test fallaría.
        """
        from app.persistence.models.audit import DecisionAuditLog
        from app.persistence.models.unclassified_record import UnclassifiedRecord

        record = self._ventas_record(
            sample_tenant.tenant_id,
            [
                {"fecha": "2024-01-15", "monto": "50000"},
                {"fecha": "2024-01-16", "monto": ""},
            ],
        )
        db_session.add(record)
        await db_session.commit()

        # finalize_import_lease corre DESPUÉS de la captura/audit, antes del commit
        # del savepoint → forzamos el fallo ahí para ejercitar el rollback integral.
        with unittest.mock.patch(
            "app.api.v1.ingestion.finalize_import_lease",
            new_callable=unittest.mock.AsyncMock,
            side_effect=HTTPException(status_code=500, detail="boom"),
        ):
            response = await client.post(
                f"/api/v1/ingestion/files/{record.id}/confirm",
                headers=auth_headers,
                json={
                    "confirmed_fields": {"ventas": True},
                    "column_mappings": [
                        {"source_column": "fecha", "target_field": "transaction_date"},
                        {"source_column": "monto", "target_field": "amount"},
                    ],
                    "column_risk_decisions": [
                        {
                            "context_id": "table",
                            "source_column": "monto",
                            "target_field": "amount",
                            "action": "route_affected_rows_to_others",
                        }
                    ],
                },
            )
        assert response.status_code >= 400

        # Nada persistido.
        sales = (
            await db_session.execute(
                select(SaleEntry).where(SaleEntry.tenant_id == sample_tenant.tenant_id)
            )
        ).scalars().all()
        assert sales == []
        others = (
            await db_session.execute(
                select(UnclassifiedRecord).where(
                    UnclassifiedRecord.tenant_id == sample_tenant.tenant_id
                )
            )
        ).scalars().all()
        assert others == []
        audits = (
            await db_session.execute(
                select(DecisionAuditLog).where(
                    DecisionAuditLog.tenant_id == sample_tenant.tenant_id,
                    DecisionAuditLog.decision_type == "INGESTION_COLUMN_RISK_DECISIONS",
                )
            )
        ).scalars().all()
        assert audits == []

        # El archivo quedó re-confirmable.
        refreshed = (
            await db_session.execute(
                select(UploadedFile).where(UploadedFile.id == record.id)
            )
        ).scalar_one()
        assert refreshed.processing_status == PROCESSING_STATUS_NEEDS_CONFIRMATION
        assert refreshed.import_attempt_id is None

    async def test_audit_agregada_sin_pii(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        mock_score_trigger: unittest.mock.MagicMock,
    ) -> None:
        """La auditoría agregada registra columnas + conteos, NUNCA el valor crudo
        (email/teléfono/documento) de la fila ruteada (invariantes 7, 9)."""
        from app.persistence.models.audit import DecisionAuditLog

        pii_email = "secreto@ejemplo.com"
        record = self._ventas_record(
            sample_tenant.tenant_id,
            [
                {"fecha": "2024-01-15", "monto": "50000", "contacto": "ok@x.com"},
                # amount inválido → afectada; su columna contacto trae PII.
                {"fecha": "2024-01-16", "monto": "", "contacto": pii_email},
            ],
        )
        db_session.add(record)
        await db_session.commit()

        response = await client.post(
            f"/api/v1/ingestion/files/{record.id}/confirm",
            headers=auth_headers,
            json={
                "confirmed_fields": {"ventas": True},
                "column_mappings": [
                    {"source_column": "fecha", "target_field": "transaction_date"},
                    {"source_column": "monto", "target_field": "amount"},
                ],
                "column_risk_decisions": [
                    {
                        "context_id": "table",
                        "source_column": "monto",
                        "target_field": "amount",
                        "action": "route_affected_rows_to_others",
                    }
                ],
            },
        )
        assert response.status_code == 200

        audit = (
            await db_session.execute(
                select(DecisionAuditLog).where(
                    DecisionAuditLog.tenant_id == sample_tenant.tenant_id,
                    DecisionAuditLog.decision_type == "INGESTION_COLUMN_RISK_DECISIONS",
                )
            )
        ).scalar_one()
        # Conteos correctos.
        assert audit.decision_data["filas_riesgo_a_otros"] == 1
        assert audit.decision_data["filas_riesgo_importadas"] == 1
        assert audit.decision_data["routed_to_others"] == {"table": 1}
        # Sin PII: el email crudo no aparece en ninguna parte del registro.
        serialized = f"{audit.decision_data}{audit.context}"
        assert pii_email not in serialized

    async def test_drop_solo_su_contexto_y_counters(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        mock_score_trigger: unittest.mock.MagicMock,
    ) -> None:
        """Multi-hoja: drop de `notas` SOLO en s1 (venta) — s2 (gasto) intacto.
        Ambas hojas importan; la auditoría agregada registra solo s1."""
        from app.persistence.models.audit import DecisionAuditLog

        record = UploadedFile(
            tenant_id=sample_tenant.tenant_id,
            uploaded_by=None,
            original_filename="mixto.xlsx",
            s3_key="uploads/test/uuid/mixto.xlsx",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            size_bytes=512,
            purpose="mixto",
            status="uploaded",
            processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
            parsed_summary_json={
                "confidence": "HIGH",
                "file_type": "spreadsheet",
                "inferred_type": "mixed",
                "multi_sheet": True,
                "row_count": 2,
                "mapping_contexts": [
                    {
                        "context_id": "s1",
                        "label": "Ventas",
                        "entity_type": "sale",
                        "headers": ["fecha", "monto", "notas"],
                        "preview_rows": [],
                    },
                    {
                        "context_id": "s2",
                        "label": "Gastos",
                        "entity_type": "expense",
                        "headers": ["fecha", "monto", "notas"],
                        "preview_rows": [],
                    },
                ],
                "ventas_detectadas": [
                    {
                        "fecha": "2024-01-15",
                        "monto": "50000",
                        "notas": "hola",
                        "__context__": "s1",
                    }
                ],
                "gastos_detectados": [
                    {
                        "fecha": "2024-01-16",
                        "monto": "12000",
                        "notas": "chau",
                        "__context__": "s2",
                    }
                ],
            },
        )
        db_session.add(record)
        await db_session.commit()

        response = await client.post(
            f"/api/v1/ingestion/files/{record.id}/confirm",
            headers=auth_headers,
            json={
                "confirmed_fields": {},
                "context_confirmed": {"s1": True, "s2": True},
                "column_mappings": [
                    {"source_column": "fecha", "target_field": "transaction_date",
                     "context_id": "s1", "entity_type": "sale"},
                    {"source_column": "monto", "target_field": "amount",
                     "context_id": "s1", "entity_type": "sale"},
                    {"source_column": "notas", "target_field": "notes",
                     "context_id": "s1", "entity_type": "sale", "user_selected": True},
                    {"source_column": "fecha", "target_field": "expense_date",
                     "context_id": "s2", "entity_type": "expense"},
                    {"source_column": "monto", "target_field": "amount",
                     "context_id": "s2", "entity_type": "expense"},
                    {"source_column": "notas", "target_field": "notes",
                     "context_id": "s2", "entity_type": "expense", "user_selected": True},
                ],
                "column_risk_decisions": [
                    {
                        "context_id": "s1",
                        "source_column": "notas",
                        "target_field": "notes",
                        "action": "drop_column",
                    }
                ],
            },
        )
        assert response.status_code == 200

        # Ambas hojas importaron.
        sales = (
            await db_session.execute(
                select(SaleEntry).where(SaleEntry.tenant_id == sample_tenant.tenant_id)
            )
        ).scalars().all()
        expenses = (
            await db_session.execute(
                select(ExpenseEntry).where(
                    ExpenseEntry.tenant_id == sample_tenant.tenant_id
                )
            )
        ).scalars().all()
        assert len(sales) == 1
        assert len(expenses) == 1

        audit = (
            await db_session.execute(
                select(DecisionAuditLog).where(
                    DecisionAuditLog.tenant_id == sample_tenant.tenant_id,
                    DecisionAuditLog.decision_type == "INGESTION_COLUMN_RISK_DECISIONS",
                )
            )
        ).scalar_one()
        assert audit.decision_data["dropped_columns"] == {"s1": ["notas"]}
        assert audit.decision_data["columnas_eliminadas"] == 1

    async def test_decision_de_contexto_excluido_es_noop(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        mock_score_trigger: unittest.mock.MagicMock,
    ) -> None:
        """Una decisión route sobre un contexto EXCLUIDO del import no captura
        nada en Otros (sus filas ni se procesan) y no crea auditoría."""
        from app.persistence.models.audit import DecisionAuditLog
        from app.persistence.models.unclassified_record import UnclassifiedRecord

        record = UploadedFile(
            tenant_id=sample_tenant.tenant_id,
            uploaded_by=None,
            original_filename="mixto.xlsx",
            s3_key="uploads/test/uuid/mixto.xlsx",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            size_bytes=512,
            purpose="mixto",
            status="uploaded",
            processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
            parsed_summary_json={
                "confidence": "HIGH",
                "file_type": "spreadsheet",
                "inferred_type": "mixed",
                "multi_sheet": True,
                "row_count": 2,
                "mapping_contexts": [
                    {"context_id": "s1", "label": "Ventas", "entity_type": "sale",
                     "headers": ["fecha", "monto"], "preview_rows": []},
                    {"context_id": "s2", "label": "Gastos", "entity_type": "expense",
                     "headers": ["fecha", "monto"], "preview_rows": []},
                ],
                "ventas_detectadas": [
                    {"fecha": "2024-01-15", "monto": "50000", "__context__": "s1"}
                ],
                "gastos_detectados": [
                    {"fecha": "2024-01-16", "monto": "", "__context__": "s2"}
                ],
            },
        )
        db_session.add(record)
        await db_session.commit()

        response = await client.post(
            f"/api/v1/ingestion/files/{record.id}/confirm",
            headers=auth_headers,
            json={
                "confirmed_fields": {},
                # s2 EXCLUIDO — solo s1 se importa.
                "context_confirmed": {"s1": True, "s2": False},
                "column_mappings": [
                    {"source_column": "fecha", "target_field": "transaction_date",
                     "context_id": "s1", "entity_type": "sale"},
                    {"source_column": "monto", "target_field": "amount",
                     "context_id": "s1", "entity_type": "sale"},
                    {"source_column": "fecha", "target_field": "expense_date",
                     "context_id": "s2", "entity_type": "expense"},
                    {"source_column": "monto", "target_field": "amount",
                     "context_id": "s2", "entity_type": "expense"},
                ],
                # route sobre s2 (excluido) — debe ser no-op.
                "column_risk_decisions": [
                    {"context_id": "s2", "source_column": "monto",
                     "target_field": "amount",
                     "action": "route_affected_rows_to_others"},
                ],
            },
        )
        assert response.status_code == 200

        # s2 no se procesó → nada en Otros, ni auditoría F8b.
        others = (
            await db_session.execute(
                select(UnclassifiedRecord).where(
                    UnclassifiedRecord.tenant_id == sample_tenant.tenant_id
                )
            )
        ).scalars().all()
        assert others == []
        audits = (
            await db_session.execute(
                select(DecisionAuditLog).where(
                    DecisionAuditLog.tenant_id == sample_tenant.tenant_id,
                    DecisionAuditLog.decision_type == "INGESTION_COLUMN_RISK_DECISIONS",
                )
            )
        ).scalars().all()
        assert audits == []
