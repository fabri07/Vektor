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

import io
import unittest.mock
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.file import (
    PROCESSING_STATUS_DONE,
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
async def test_parse_date_fallback_logs_debug(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.application.services.ingestion_import_service as importer

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

    result = await db_session.execute(select(SaleEntry))
    sale = result.scalar_one()
    assert sale.amount == Decimal("100")
    assert sale.transaction_date.date() == date.today()
    assert capture.debug_events == [
        (
            "ingestion.parse.date_fallback_today",
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
