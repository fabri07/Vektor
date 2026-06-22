"""Tests para la extracción de remito por archivo.

Cubre:
- Servicio determinístico (XLSX/CSV): mapea columnas a líneas sin IA ni red.
- Servicio IA (foto/PDF): mockea el cliente Anthropic y verifica que se arma la
  request multimodal (image/document block + tool_use forzado) y se parsea la
  respuesta estructurada a ``RemitoExtraction``.
- Endpoint ``POST /suppliers/{id}/receipts/extract``: 404 (otro tenant), 400
  (sentinela), 200 (happy path con planilla).
"""

from __future__ import annotations

import io
import unittest.mock
from typing import Any

import openpyxl
import pytest
from httpx import AsyncClient

from app.application.services.remito_extraction_service import (
    RemitoExtraction,
    extract_remito,
)


def _csv_remito() -> bytes:
    return (
        "producto,cantidad,precio_unitario\n"
        "Yerba Playadito,10,800.00\n"
        "Azúcar Ledesma,5,600.50\n"
    ).encode()


def _xlsx_remito() -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["sku", "producto", "cantidad", "costo_unitario"])
    ws.append(["YP-001", "Yerba Playadito", 10, 800])
    ws.append(["AL-002", "Azúcar Ledesma", 5, 600])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class _FakeBlock:
    def __init__(self, *, type_: str, **kwargs: Any) -> None:
        self.type = type_
        for k, v in kwargs.items():
            setattr(self, k, v)


class _FakeUsage:
    input_tokens = 1200
    output_tokens = 80


class _FakeResponse:
    def __init__(self, tool_input: dict[str, Any]) -> None:
        self.content = [_FakeBlock(type_="tool_use", input=tool_input)]
        self.usage = _FakeUsage()


def _mock_anthropic_factory(tool_input: dict[str, Any]) -> tuple[Any, unittest.mock.AsyncMock]:
    """Crea un factory que devuelve un cliente con messages.create mockeado."""
    create = unittest.mock.AsyncMock(return_value=_FakeResponse(tool_input))
    client = unittest.mock.MagicMock()
    client.messages.create = create

    def factory(*_args: Any, **_kwargs: Any) -> Any:
        return client

    return factory, create


# ── Servicio determinístico ───────────────────────────────────────────────────


@pytest.mark.asyncio
class TestTabularExtraction:
    async def test_csv_extracts_lines(self) -> None:
        extraction, usage = await extract_remito(_csv_remito(), "remito.csv")
        assert usage is None  # sin IA
        assert extraction.confidence == "HIGH"
        names = {ln.product_name for ln in extraction.lines}
        assert names == {"Yerba Playadito", "Azúcar Ledesma"}
        yerba = next(ln for ln in extraction.lines if ln.product_name == "Yerba Playadito")
        assert yerba.qty == 10.0
        assert yerba.unit_price == 800.0

    async def test_xlsx_extracts_lines_with_sku(self) -> None:
        extraction, usage = await extract_remito(_xlsx_remito(), "remito.xlsx")
        assert usage is None
        assert extraction.confidence == "HIGH"
        by_name = {ln.product_name: ln for ln in extraction.lines}
        assert by_name["Yerba Playadito"].sku == "YP-001"
        assert by_name["Azúcar Ledesma"].qty == 5.0
        assert by_name["Azúcar Ledesma"].unit_price == 600.0

    async def test_csv_ar_number_format(self) -> None:
        content = (
            b"producto,cantidad,precio_unitario\n"
            b"Fideos,3,\"$ 1.250,75\"\n"
        )
        extraction, _ = await extract_remito(content, "remito.csv")
        assert extraction.lines[0].unit_price == 1250.75

    async def test_unrecognized_columns_lower_confidence(self) -> None:
        content = b"foo,bar,baz\n1,2,3\n"
        extraction, _ = await extract_remito(content, "remito.csv")
        # Sin columna de producto reconocible: sin líneas, warnings explican.
        assert extraction.lines == []
        assert extraction.confidence == "LOW"
        assert extraction.warnings


# ── Servicio IA (foto / PDF) ──────────────────────────────────────────────────


@pytest.mark.asyncio
class TestAIExtraction:
    _TOOL_INPUT = {
        "lines": [
            {"product_name": "Coca Cola 1.5L", "sku": None, "qty": 24, "unit_price": 950},
            {"product_name": "Sprite 1.5L", "sku": "SP-15", "qty": 12, "unit_price": 900},
        ],
        "shipping_cost": 2000,
    }

    async def test_image_builds_multimodal_request_and_parses(self) -> None:
        factory, create = _mock_anthropic_factory(self._TOOL_INPUT)
        # PNG mágico mínimo para que detect_supported_mime lo reconozca.
        png = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 64
        extraction, usage = await extract_remito(
            png, "remito.png", client_factory=factory
        )
        assert usage == {"input_tokens": 1200, "output_tokens": 80}
        assert len(extraction.lines) == 2
        assert extraction.shipping_cost == 2000.0

        # La request fue multimodal con tool_use forzado.
        kwargs = create.call_args.kwargs
        assert kwargs["model"] == "claude-sonnet-4-6"
        assert kwargs["tool_choice"]["type"] == "tool"
        blocks = kwargs["messages"][0]["content"]
        assert blocks[0]["type"] == "image"
        assert blocks[0]["source"]["type"] == "base64"

    async def test_pdf_uses_document_block(self) -> None:
        factory, create = _mock_anthropic_factory(self._TOOL_INPUT)
        pdf = b"%PDF-1.4\n" + b"0" * 64
        extraction, usage = await extract_remito(pdf, "remito.pdf", client_factory=factory)
        assert usage is not None
        assert len(extraction.lines) == 2
        blocks = create.call_args.kwargs["messages"][0]["content"]
        assert blocks[0]["type"] == "document"
        assert blocks[0]["source"]["media_type"] == "application/pdf"

    async def test_ai_no_lines_low_confidence(self) -> None:
        factory, _ = _mock_anthropic_factory({"lines": []})
        png = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 64
        extraction, _ = await extract_remito(png, "remito.png", client_factory=factory)
        assert extraction.lines == []
        assert extraction.confidence == "LOW"
        assert extraction.warnings

    async def test_user_hint_wrapped(self) -> None:
        factory, create = _mock_anthropic_factory(self._TOOL_INPUT)
        png = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 64
        await extract_remito(
            png, "remito.png", user_hint="es de Distribuidora Norte", client_factory=factory
        )
        text_block = create.call_args.kwargs["messages"][0]["content"][1]["text"]
        assert "<user_message>" in text_block

    async def test_unsupported_format_returns_warning(self) -> None:
        # docx: formato válido para el pipeline pero no para extracción de remito.
        extraction, usage = await extract_remito(b"not a real file", "nota.txt")
        assert usage is None
        assert extraction.lines == []
        assert extraction.warnings


# ── Endpoint ──────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_s3_upload():
    with unittest.mock.patch(
        "app.integrations.s3.S3Client.upload",
        new_callable=unittest.mock.AsyncMock,
        return_value="tenants/test/remito-file",
    ) as mock:
        yield mock


@pytest.mark.asyncio
class TestExtractEndpoint:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def _make_supplier(self, client: AsyncClient, headers: dict[str, Any]) -> str:
        resp = await client.post(
            "/api/v1/suppliers", json={"name": "Mayorista Extract"}, headers=headers
        )
        return str(resp.json()["id"])

    async def test_extract_happy_path_csv(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        mock_s3_upload: unittest.mock.AsyncMock,
    ) -> None:
        sid = await self._make_supplier(client, auth_headers)
        resp = await client.post(
            f"/api/v1/suppliers/{sid}/receipts/extract",
            files={"file": ("remito.csv", _csv_remito(), "text/csv")},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["confidence"] == "HIGH"
        assert len(body["lines"]) == 2
        # unit_price serializa como número, no string.
        assert isinstance(body["lines"][0]["unit_price"], int | float)
        assert body["source_upload_id"] is not None
        mock_s3_upload.assert_called()

    async def test_extract_rejected_against_sentinel(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        created = await client.post(
            "/api/v1/suppliers",
            json={"name": "No identificado", "custom_fields": {"_sentinel": "true"}},
            headers=auth_headers,
        )
        sid = created.json()["id"]
        resp = await client.post(
            f"/api/v1/suppliers/{sid}/receipts/extract",
            files={"file": ("remito.csv", _csv_remito(), "text/csv")},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    async def test_extract_other_tenant_404(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        second_auth_headers: dict[str, Any],
    ) -> None:
        sid = await self._make_supplier(client, auth_headers)
        resp = await client.post(
            f"/api/v1/suppliers/{sid}/receipts/extract",
            files={"file": ("remito.csv", _csv_remito(), "text/csv")},
            headers=second_auth_headers,
        )
        assert resp.status_code == 404

    async def test_extract_unknown_supplier_404(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        import uuid

        resp = await client.post(
            f"/api/v1/suppliers/{uuid.uuid4()}/receipts/extract",
            files={"file": ("remito.csv", _csv_remito(), "text/csv")},
            headers=auth_headers,
        )
        assert resp.status_code == 404


def test_remito_extraction_dataclass_defaults() -> None:
    """Sanity: el dataclass tiene defaults seguros (lines vacío, ARS, LOW)."""
    ext = RemitoExtraction()
    assert ext.lines == []
    assert ext.currency == "ARS"
    assert ext.confidence == "LOW"
