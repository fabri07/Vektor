"""Unit tests for shared file parsing helpers."""

from __future__ import annotations

import pytest

from app.application.services.file_parsing import (
    detect_supported_mime,
    infer_spreadsheet_type,
    parse_uploaded_content,
)


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


# --- infer_spreadsheet_type ---

def test_product_csv_with_date_and_price_classified_as_stock() -> None:
    """Bug regression: fecha + nombre + precio → stock, no ventas."""
    result = infer_spreadsheet_type(
        has_fecha=True,
        has_venta=False,
        has_gasto=False,
        has_producto=True,
        has_precio_ambiguo=True,
        has_nombre=True,
    )
    assert result == "stock"


def test_product_csv_with_date_and_explicit_venta_col_classified_as_stock() -> None:
    """fecha + nombre + importe (VENTA_COL) → stock porque señal de nombre gana."""
    result = infer_spreadsheet_type(
        has_fecha=True,
        has_venta=True,
        has_gasto=False,
        has_producto=True,
        has_precio_ambiguo=False,
        has_nombre=True,
    )
    assert result == "stock"


def test_descripcion_col_alone_does_not_trigger_stock() -> None:
    """fecha + monto + descripcion → ventas (descripcion es señal débil, no es NOMBRE_COLS)."""
    result = infer_spreadsheet_type(
        has_fecha=True,
        has_venta=True,
        has_gasto=False,
        has_producto=True,   # descripcion está en PRODUCTO_COLS pero no en NOMBRE_COLS/CATALOGO_COLS
        has_precio_ambiguo=False,
        has_catalogo_fuerte=False,
        has_nombre=False,    # sin nombre/producto explícito
    )
    assert result == "ventas"


def test_sku_col_always_classified_as_stock() -> None:
    """sku + fecha + monto → stock (señal fuerte de catálogo)."""
    result = infer_spreadsheet_type(
        has_fecha=True,
        has_venta=True,
        has_gasto=False,
        has_producto=True,
        has_precio_ambiguo=False,
        has_catalogo_fuerte=True,
    )
    assert result == "stock"


def test_product_csv_no_date_classified_as_stock() -> None:
    """nombre + precio + stock → stock."""
    result = infer_spreadsheet_type(
        has_fecha=False,
        has_venta=False,
        has_gasto=False,
        has_producto=True,
        has_precio_ambiguo=True,
        has_nombre=True,
    )
    assert result == "stock"


def test_sale_csv_without_product_classified_as_ventas() -> None:
    """fecha + monto (sin columna de producto) → ventas."""
    result = infer_spreadsheet_type(
        has_fecha=True,
        has_venta=True,
        has_gasto=False,
        has_producto=False,
        has_precio_ambiguo=False,
    )
    assert result == "ventas"


def test_sale_csv_ambiguous_price_without_product_classified_as_ventas() -> None:
    """Solo precio ambiguo sin producto ni fecha → ventas (fallback)."""
    result = infer_spreadsheet_type(
        has_fecha=False,
        has_venta=False,
        has_gasto=False,
        has_producto=False,
        has_precio_ambiguo=True,
    )
    assert result == "ventas"


def test_product_csv_parse_with_date_and_price_infers_stock(csv_bytes: bytes) -> None:
    """CSV con columnas fecha+nombre+precio → inferred_type='stock' (bug regression end-to-end)."""
    product_csv = (
        b"fecha,nombre,precio\n"
        b"2024-01-15,Coca-Cola 600ml,500\n"
        b"2024-01-15,Agua 1.5L,300\n"
    )
    summary = parse_uploaded_content(product_csv, "text/csv", "productos.csv")
    assert summary["inferred_type"] == "stock"
    assert summary["confidence"] == "MEDIUM"


def test_sale_csv_with_descripcion_stays_ventas() -> None:
    """CSV fecha+monto+descripcion (ventas reales) → no regresión: sigue siendo ventas."""
    sales_csv = (
        b"fecha,monto,descripcion\n"
        b"2024-01-15,50000,Venta del dia\n"
        b"2024-01-16,35000,Venta tarde\n"
    )
    summary = parse_uploaded_content(sales_csv, "text/csv", "ventas.csv")
    assert summary["inferred_type"] == "ventas"
