"""Unit tests for shared file parsing helpers."""

from __future__ import annotations

import pytest

from app.application.services.file_parsing import detect_supported_mime, parse_uploaded_content


def test_detect_supported_mime_uses_extension_fallback_for_txt(txt_bytes: bytes) -> None:
    mime = detect_supported_mime(txt_bytes, "notas.txt")
    assert mime == "text/plain"


def test_parse_csv_builds_canonical_summary(csv_bytes: bytes) -> None:
    summary = parse_uploaded_content(csv_bytes, "text/csv", "ventas.csv")
    assert summary["file_type"] == "spreadsheet"
    assert summary["source_format"] == "csv"
    assert summary["row_count"] == 2
    assert summary["columns"] == ["fecha", "monto", "descripcion"]
    assert len(summary["preview_rows"]) >= 1


def test_parse_txt_builds_text_summary(txt_bytes: bytes) -> None:
    summary = parse_uploaded_content(txt_bytes, "text/plain", "notas.txt")
    assert summary["file_type"] == "text"
    assert summary["source_format"] == "txt"
    assert "Venta del dia" in summary["raw_text_preview"]
    assert isinstance(summary["preview_rows"], list)


def test_parse_docx_builds_text_summary(docx_bytes: bytes) -> None:
    summary = parse_uploaded_content(
        docx_bytes,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "resumen.docx",
    )
    assert summary["file_type"] == "text"
    assert summary["source_format"] == "docx"
    assert "Venta del dia" in summary["raw_text_preview"]


def test_parse_pptx_builds_text_summary(pptx_bytes: bytes) -> None:
    summary = parse_uploaded_content(
        pptx_bytes,
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "slides.pptx",
    )
    assert summary["file_type"] == "text"
    assert summary["source_format"] == "pptx"
    assert "Resumen financiero" in summary["raw_text_preview"]


def test_parse_pdf_does_not_crash(pdf_bytes: bytes) -> None:
    summary = parse_uploaded_content(pdf_bytes, "application/pdf", "reporte.pdf")
    assert summary["file_type"] == "text"
    assert summary["source_format"] == "pdf"
    assert "raw_text_preview" in summary


def test_parse_png_returns_image_summary(png_bytes: bytes) -> None:
    summary = parse_uploaded_content(png_bytes, "image/png", "ticket.png")
    assert summary["file_type"] == "image"
    assert summary["source_format"] == "png"


def test_detect_supported_mime_rejects_unknown_binary() -> None:
    with pytest.raises(ValueError):
        detect_supported_mime(b"random-binary", "payload.bin")
