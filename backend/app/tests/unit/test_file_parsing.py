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
        has_producto=True,  # descripcion está en PRODUCTO_COLS pero no en NOMBRE_COLS/CATALOGO_COLS
        has_precio_ambiguo=False,
        has_catalogo_fuerte=False,
        has_nombre=False,  # sin nombre/producto explícito
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


def test_ambiguous_price_without_strong_signal_is_general() -> None:
    """FASE 3: solo precio/dinero sin señal fuerte de venta/gasto → ambiguo (general),
    NO venta silenciosa. El usuario confirma el tipo."""
    result = infer_spreadsheet_type(
        has_fecha=False,
        has_venta=False,
        has_gasto=False,
        has_producto=False,
        has_precio_ambiguo=True,
    )
    assert result == "general"


def test_product_csv_parse_with_date_and_price_infers_stock(csv_bytes: bytes) -> None:
    """CSV con columnas fecha+nombre+precio → inferred_type='stock' (bug regression end-to-end)."""
    product_csv = (
        b"fecha,nombre,precio\n" b"2024-01-15,Coca-Cola 600ml,500\n" b"2024-01-15,Agua 1.5L,300\n"
    )
    summary = parse_uploaded_content(product_csv, "text/csv", "productos.csv")
    assert summary["inferred_type"] == "stock"
    assert summary["confidence"] == "MEDIUM"


def test_csv_fecha_monto_descripcion_is_ambiguous() -> None:
    """FASE 3: fecha+monto+descripcion (sin señal fuerte de venta NI gasto) → ambiguo
    ('general'). 'monto' es dinero genérico y no decide el tipo solo; el usuario confirma."""
    csv = (
        b"fecha,monto,descripcion\n"
        b"2024-01-15,50000,Pago varios\n"
        b"2024-01-16,35000,Otro\n"
    )
    summary = parse_uploaded_content(csv, "text/csv", "doc.csv")
    assert summary["inferred_type"] == "general"


def test_csv_with_strong_venta_signal_is_ventas() -> None:
    """fecha+cliente+monto (señal fuerte de venta) → ventas."""
    csv = b"fecha,cliente,monto\n2024-01-15,Juan,50000\n"
    summary = parse_uploaded_content(csv, "text/csv", "ventas.csv")
    assert summary["inferred_type"] == "ventas"


_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _build_multisheet_xlsx(sheets: dict[str, list[list[object]]]) -> bytes:
    """Construye un xlsx en memoria con varias hojas. sheets = {nombre: filas}."""
    import io as _io

    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    wb.remove(wb.active)  # sacar la hoja por defecto vacía
    for name, rows in sheets.items():
        ws = wb.create_sheet(title=name)
        for row in rows:
            ws.append(row)
    buf = _io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_multisheet_compras_routed_to_stock_not_gastos() -> None:
    """Regresión: una hoja 'Compras Mercadería' debe ir a stock (inventario), no a gastos.

    Antes, _classify_sheet ruteaba 'compra'/'mercaderia' a gastos, por lo que las
    compras nunca llegaban a productos.
    """
    content = _build_multisheet_xlsx(
        {
            "Ventas": [
                ["fecha", "total", "producto", "cantidad"],
                ["2024-01-15", "5400", "Gomitas", "2"],
            ],
            "Gastos Operativos": [
                ["fecha", "monto", "categoria", "descripcion"],
                ["2024-01-15", "12000", "alquiler", "Alquiler local"],
            ],
            "Compras Mercadería": [
                ["producto", "costo_unitario", "cantidad"],
                ["Coca-Cola 600ml", "800", "24"],
            ],
        }
    )
    summary = parse_uploaded_content(content, _XLSX_MIME, "mixto.xlsx")

    assert summary.get("multi_sheet") is True
    # Ventas → ventas_detectadas; Gastos operativos → gastos_detectados;
    # Compras → stock_detectado (NO gastos).
    assert len(summary["ventas_detectadas"]) == 1
    assert len(summary["gastos_detectados"]) == 1
    assert len(summary["stock_detectado"]) == 1
    assert summary["stock_detectado"][0]["producto"] == "Coca-Cola 600ml"
    # La compra no debe haber caído en gastos.
    assert all(
        "Coca-Cola" not in str(r.values()) for r in summary["gastos_detectados"]
    )


def test_multisheet_exposes_mapping_contexts() -> None:
    """Multi-hoja expone un mapping_context por pestaña con entity_type + headers,
    y cada fila detectada lleva el marcador __context__."""
    content = _build_multisheet_xlsx(
        {
            "Ventas": [["fecha", "total", "producto"], ["2024-01-15", "5400", "Gomitas"]],
            "Gastos Operativos": [
                ["fecha", "monto", "categoria"],
                ["2024-01-15", "12000", "alquiler"],
            ],
        }
    )
    summary = parse_uploaded_content(content, _XLSX_MIME, "mixto.xlsx")

    contexts = summary["mapping_contexts"]
    assert len(contexts) == 2
    by_id = {c["context_id"]: c for c in contexts}
    assert by_id["sheet:Ventas"]["entity_type"] == "sale"
    assert by_id["sheet:Ventas"]["source_kind"] == "sheet"
    assert by_id["sheet:Ventas"]["headers"] == ["fecha", "total", "producto"]
    assert by_id["sheet:Gastos Operativos"]["entity_type"] == "expense"

    # Cada fila detectada tiene el marcador de contexto.
    assert summary["ventas_detectadas"][0]["__context__"] == "sheet:Ventas"
    assert summary["gastos_detectados"][0]["__context__"] == "sheet:Gastos Operativos"
    # El preview global no lo expone.
    assert all("__context__" not in r for r in summary["preview_rows"])


def test_csv_exposes_single_table_context() -> None:
    """CSV expone un único mapping_context 'table' con su entity_type inferido."""
    sales_csv = b"fecha,venta,cliente\n2024-01-15,50000,Juan\n"
    summary = parse_uploaded_content(sales_csv, "text/csv", "ventas.csv")
    contexts = summary["mapping_contexts"]
    assert len(contexts) == 1
    assert contexts[0]["context_id"] == "table"
    assert contexts[0]["source_kind"] == "table"
    assert contexts[0]["entity_type"] == "sale"
    assert contexts[0]["headers"] == ["fecha", "venta", "cliente"]


def test_build_text_contexts_groups_without_headers() -> None:
    """Documentos de texto: contexts por grupo, headers=None, filas marcadas."""
    from app.application.services.file_parsing import _build_text_contexts

    detected = {
        "ventas_detectadas": [{"linea": "Venta 5000", "montos": ["5000"]}],
        "gastos_detectados": [{"linea": "Pago luz 3000", "montos": ["3000"]}],
        "stock_detectado": [],
    }
    contexts = _build_text_contexts(detected, "text_group")
    assert {c["context_id"] for c in contexts} == {"text:sale", "text:expense"}
    sale_ctx = next(c for c in contexts if c["context_id"] == "text:sale")
    assert sale_ctx["headers"] is None  # texto no tiene columnas
    assert sale_ctx["source_kind"] == "text_group"
    assert "linea" in sale_ctx["fields"]
    # Las filas quedan marcadas con __context__; el preview no lo expone.
    assert detected["ventas_detectadas"][0]["__context__"] == "text:sale"
    assert all("__context__" not in r for r in sale_ctx["preview_rows"])
