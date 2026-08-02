"""Unit tests for shared file parsing helpers."""

from __future__ import annotations

import pytest

from app.application.services.file_parsing import (
    analyze_headers,
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


def test_product_csv_with_date_and_venta_context_but_no_amount_is_stock() -> None:
    """fecha + nombre + contexto de venta, SIN monto de operación → stock.

    (Antes el docstring decía "importe", que sí es un monto de operación
    —está en TRANSACCION_MONTO_COLS— pero el test nunca lo pasó: la evidencia
    transaccional está incompleta y por eso la señal de nombre gana. El caso con
    el monto presente es el test de abajo.)
    """
    result = infer_spreadsheet_type(
        has_fecha=True,
        has_venta=True,
        has_gasto=False,
        has_producto=True,
        has_precio_ambiguo=False,
        has_nombre=True,
        has_monto_transaccion=False,
    )
    assert result == "stock"


def test_product_col_with_full_transactional_evidence_is_ventas() -> None:
    """fecha + monto de la operación + contexto de venta → ventas, aunque haya
    columna de producto. Una exportación de ventas trae las tres señales juntas;
    una lista de precios no llega nunca a tenerlas."""
    result = infer_spreadsheet_type(
        has_fecha=True,
        has_venta=True,
        has_gasto=False,
        has_producto=True,
        has_precio_ambiguo=False,
        has_nombre=True,
        has_monto_transaccion=True,
        venta_score=1,
    )
    assert result == "ventas"


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


def test_libro_compras_with_sku_classified_as_gastos_not_stock() -> None:
    """Bug regression: un LIBRO DE COMPRAS de mercadería (monto de transacción + forma
    de pago + proveedor + fecha) debe clasificarse como gastos AUNQUE traiga
    sku/cantidad/costo_unitario. Antes, `has_catalogo_fuerte` (sku) ganaba y se perdía
    el COGS y la salida de caja."""
    result = infer_spreadsheet_type(
        has_fecha=True,
        has_venta=False,
        has_gasto=True,
        has_producto=True,
        has_precio_ambiguo=True,  # "total" presente
        has_catalogo_fuerte=True,  # sku presente
        has_nombre=True,  # producto presente
        has_cantidad=True,  # cantidad presente
        has_monto_transaccion=True,  # total
        has_forma_pago=True,  # forma_pago (venta_score +1)
        has_proveedor=True,  # proveedor (gasto_score +1)
        # proveedor + categoria + costo_unitario → 3 señales de gasto; forma_pago → 1 de venta
        gasto_score=3,
        venta_score=1,
    )
    assert result == "gastos"


def test_real_catalogo_still_classified_as_stock() -> None:
    """Un catálogo real (sku+nombre+precio_venta+stock_actual+stock_minimo, sin
    forma_pago/proveedor/monto de transacción) sigue siendo stock — el guard de
    libro de compras NO se dispara."""
    result = infer_spreadsheet_type(
        has_fecha=False,
        has_venta=False,
        has_gasto=False,
        has_producto=True,
        has_precio_ambiguo=False,
        has_catalogo_fuerte=True,  # sku
        has_nombre=True,  # nombre
        has_monto_transaccion=False,
        has_forma_pago=False,
        has_proveedor=False,
    )
    assert result == "stock"


def test_csv_libro_compras_end_to_end_infers_gastos() -> None:
    """End-to-end: el CSV del libro de compras reportado se rutea a gastos, no stock."""
    csv = (
        b"fecha,sku,producto,cantidad,costo_unitario,total,proveedor,forma_pago,categoria\n"
        b"2024-01-15,SKU001,Coca-Cola 600ml,24,800,19200,Distribuidora SA,transferencia,Bebidas\n"
        b"2024-01-16,SKU002,Agua 1.5L,12,300,3600,Distribuidora SA,efectivo,Bebidas\n"
    )
    summary = parse_uploaded_content(csv, "text/csv", "libro_compras.csv")
    assert summary["inferred_type"] == "gastos"


def test_csv_catalogo_end_to_end_infers_stock() -> None:
    """End-to-end: un catálogo de productos sigue clasificándose como stock."""
    csv = (
        b"sku,nombre,precio_venta,stock_actual,stock_minimo\n"
        b"SKU001,Coca-Cola 600ml,1200,48,10\n"
        b"SKU002,Agua 1.5L,600,24,6\n"
    )
    summary = parse_uploaded_content(csv, "text/csv", "catalogo.csv")
    assert summary["inferred_type"] == "stock"


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

    Content-first: la hoja de ventas lleva una señal de venta real (cliente); un
    catálogo de compras (producto/cantidad sin operación) cae a stock por contenido.
    """
    content = _build_multisheet_xlsx(
        {
            "Ventas": [
                ["fecha", "total", "cliente", "cantidad"],
                ["2024-01-15", "5400", "Juan Pérez", "2"],
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
            "Ventas": [["fecha", "total", "cliente"], ["2024-01-15", "5400", "Juan Pérez"]],
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
    assert by_id["sheet:Ventas"]["headers"] == ["fecha", "total", "cliente"]
    assert by_id["sheet:Gastos Operativos"]["entity_type"] == "expense"

    # Cada fila detectada tiene el marcador de contexto.
    assert summary["ventas_detectadas"][0]["__context__"] == "sheet:Ventas"
    assert summary["gastos_detectados"][0]["__context__"] == "sheet:Gastos Operativos"
    # El preview global no lo expone.
    assert all("__context__" not in r for r in summary["preview_rows"])


def test_multisheet_content_first_proveedores_y_stock_is_product() -> None:
    """Fix central (caso real ASTERIA): una hoja llamada 'Proveedores y Stock' que en
    realidad es un CATÁLOGO de productos se rutea por su CONTENIDO (columnas) como
    producto, NO como gasto por el 'proveedor' del nombre.

    Antes, _classify_sheet matcheaba 'proveedor'→gastos por el nombre y pisaba la
    inferencia por columnas, perdiendo los productos con su costo/stock.
    """
    content = _build_multisheet_xlsx(
        {
            "Proveedores y Stock": [
                ["Tienda", "Productos", "Stock", "Precio de compra", "Precio de venta final"],
                ["Local Centro", "Coca-Cola 600ml", "24", "800", "1200"],
            ],
            "Ventas": [["fecha", "total", "cliente"], ["2024-01-15", "5400", "Juan"]],
        }
    )
    summary = parse_uploaded_content(content, _XLSX_MIME, "asteria.xlsx")

    contexts = summary["mapping_contexts"]
    by_id = {c["context_id"]: c for c in contexts}
    ctx = by_id["sheet:Proveedores y Stock"]
    # El CONTENIDO manda: es un catálogo → product, no expense.
    assert ctx["entity_type"] == "product"
    assert summary["stock_detectado"][0]["Productos"] == "Coca-Cola 600ml"
    # No debe haber caído en gastos por el nombre de la hoja.
    assert not summary.get("gastos_detectados")


def test_multisheet_content_first_gastos_sheet_is_expense() -> None:
    """Una hoja 'Gastos' con columnas de gasto (proveedor/categoria/concepto) → expense."""
    content = _build_multisheet_xlsx(
        {
            "Gastos": [
                ["fecha", "proveedor", "monto", "categoria", "concepto"],
                ["2024-01-15", "Distribuidora SA", "12000", "alquiler", "Alquiler local"],
            ],
            "Ventas": [["fecha", "total", "cliente"], ["2024-01-15", "5400", "Juan"]],
        }
    )
    summary = parse_uploaded_content(content, _XLSX_MIME, "gastos.xlsx")

    by_id = {c["context_id"]: c for c in summary["mapping_contexts"]}
    assert by_id["sheet:Gastos"]["entity_type"] == "expense"
    assert len(summary["gastos_detectados"]) == 1


def test_multisheet_content_first_ambiguous_name_catalog_columns_is_product() -> None:
    """Nombre de hoja sin señal ('Hoja1') pero columnas claras de catálogo → product.
    El contenido decide aunque el nombre no oriente."""
    content = _build_multisheet_xlsx(
        {
            "Hoja1": [
                ["Articulo", "SKU", "Stock", "Precio"],
                ["Lapicera azul", "LAP-001", "50", "350"],
            ],
            "Ventas": [["fecha", "total", "cliente"], ["2024-01-15", "5400", "Juan"]],
        }
    )
    summary = parse_uploaded_content(content, _XLSX_MIME, "hoja1.xlsx")

    by_id = {c["context_id"]: c for c in summary["mapping_contexts"]}
    assert by_id["sheet:Hoja1"]["entity_type"] == "product"
    assert len(summary["stock_detectado"]) == 1


def test_multisheet_content_first_libro_compras_still_expense() -> None:
    """Regresión (regla -1 intacta): un libro de compras (sku/producto + monto de
    transacción + fecha + forma_pago/proveedor, contexto de gasto dominante) sigue
    ruteándose a gastos pese a traer columnas de catálogo."""
    content = _build_multisheet_xlsx(
        {
            "Compras a proveedores": [
                [
                    "sku", "producto", "monto_total", "fecha",
                    "forma_pago", "proveedor", "concepto", "categoria",
                ],
                [
                    "CC-600", "Coca-Cola 600ml", "19200", "2024-01-15",
                    "transferencia", "Distribuidora SA", "compra mercaderia", "mercaderia",
                ],
            ],
            "Ventas": [["fecha", "total", "cliente"], ["2024-01-15", "5400", "Juan"]],
        }
    )
    summary = parse_uploaded_content(content, _XLSX_MIME, "compras.xlsx")

    by_id = {c["context_id"]: c for c in summary["mapping_contexts"]}
    assert by_id["sheet:Compras a proveedores"]["entity_type"] == "expense"
    assert len(summary["gastos_detectados"]) == 1
    assert not summary.get("stock_detectado")


# --- F7a: maestros de clientes/proveedores ---


def test_cliente_maestro_headers_classified_as_clientes() -> None:
    """dni (CLIENTE_SIGNAL_COLS) sin señales transaccionales → clientes."""
    result = infer_spreadsheet_type(
        has_fecha=False,
        has_venta=False,
        has_gasto=False,
        has_producto=False,
        has_nombre=True,
        cliente_score=1,
        proveedor_master_score=0,
        has_identidad_contacto=True,
    )
    assert result == "clientes"


def test_proveedor_maestro_headers_classified_as_proveedores() -> None:
    """cuil (PROVEEDOR_MASTER_COLS) sin señales transaccionales → proveedores."""
    result = infer_spreadsheet_type(
        has_fecha=False,
        has_venta=False,
        has_gasto=False,
        has_producto=False,
        has_nombre=True,
        cliente_score=0,
        proveedor_master_score=1,
        has_identidad_contacto=True,
    )
    assert result == "proveedores"


def test_maestro_signal_with_transaccion_does_not_intercept() -> None:
    """Señal de maestro + monto/fecha (operación real) → NO se intercepta acá;
    sigue el flujo normal de venta/gasto (disjunto con la regla -1/-0.5)."""
    result = infer_spreadsheet_type(
        has_fecha=True,
        has_venta=False,
        has_gasto=True,
        has_producto=False,
        has_nombre=False,
        cliente_score=1,
        proveedor_master_score=0,
        has_monto_transaccion=True,
        gasto_score=1,
    )
    assert result == "gastos"


def test_maestro_signal_with_catalogo_fuerte_does_not_intercept() -> None:
    """Review fix: catálogo fuerte (sku/codigo/articulo/item) + columna proveedor/
    cliente, SIN cantidad/monto/fecha → sigue siendo 'stock' (regla 1), no se
    reclasifica como maestro. Un catálogo con una columna 'proveedor' (quién lo
    distribuye) no es un maestro de proveedores."""
    result = infer_spreadsheet_type(
        has_fecha=False,
        has_venta=False,
        has_gasto=False,
        has_producto=True,
        has_nombre=True,
        has_catalogo_fuerte=True,
        proveedor_master_score=1,
    )
    assert result == "stock"


def test_analyze_headers_catalogo_con_columna_proveedor_sigue_stock() -> None:
    """No-regresión (review): los 3 casos reportados — catálogo real con una columna
    de proveedor/cliente pero sin señales transaccionales sigue siendo 'stock'."""
    assert analyze_headers(["sku", "producto", "proveedor", "precio"])["inferred_type"] == "stock"
    assert analyze_headers(["codigo", "articulo", "contacto"])["inferred_type"] == "stock"
    assert analyze_headers(["articulo", "sku", "cliente"])["inferred_type"] == "stock"


def test_csv_clientes_master_end_to_end_infers_clientes() -> None:
    """End-to-end: un maestro de clientes (nombre/dni/cuit/email, sin montos) → clientes."""
    csv = (
        b"nombre,dni,cuit,email\n"
        b"Juan Perez,30111222,20301112223,juan@mail.com\n"
        b"Maria Lopez,28555666,27285556669,maria@mail.com\n"
    )
    summary = parse_uploaded_content(csv, "text/csv", "clientes.csv")
    assert summary["inferred_type"] == "clientes"
    assert len(summary["clientes_detectados"]) == 2
    assert not summary["ventas_detectadas"]


def test_csv_proveedores_master_end_to_end_infers_proveedores() -> None:
    """End-to-end: un maestro de proveedores (nombre/cuil/email/telefono, sin montos)
    → proveedores, no gastos (aunque 'proveedor' históricamente disparaba GASTO_SIGNAL_COLS)."""
    csv = (
        b"nombre,cuil,email,telefono\n"
        b"Distribuidora SA,20304050607,ventas@distribuidora.com,1145678900\n"
    )
    summary = parse_uploaded_content(csv, "text/csv", "proveedores.csv")
    assert summary["inferred_type"] == "proveedores"
    assert len(summary["proveedores_detectados"]) == 1
    assert not summary["gastos_detectados"]


def test_csv_libro_compras_still_gastos_not_proveedores() -> None:
    """No-regresión: un libro de compras (monto+forma_pago+fecha+proveedor) sigue
    clasificando 'gastos', no 'proveedores' (la regla -0.5 exige AUSENCIA de monto/fecha)."""
    csv = (
        b"fecha,sku,producto,cantidad,costo_unitario,total,proveedor,forma_pago,categoria\n"
        b"2024-01-15,SKU001,Coca-Cola 600ml,24,800,19200,Distribuidora SA,transferencia,Bebidas\n"
    )
    summary = parse_uploaded_content(csv, "text/csv", "libro_compras.csv")
    assert summary["inferred_type"] == "gastos"


def test_multisheet_classify_sheet_maps_clientes_and_proveedores_by_name() -> None:
    """_classify_sheet (desempate por NOMBRE, solo cuando el contenido es ambiguo):
    hojas 'Clientes'/'Proveedores' con columnas de contacto genéricas (sin señal
    fuerte, contenido 'general') se mapean por su nombre."""
    content = _build_multisheet_xlsx(
        {
            "Clientes": [["Email", "Teléfono"], ["juan@mail.com", "1145678900"]],
            "Proveedores": [["Email", "Teléfono"], ["ventas@dist.com", "1145670000"]],
            "Ventas": [["fecha", "total", "cliente"], ["2024-01-15", "5400", "Juan"]],
        }
    )
    summary = parse_uploaded_content(content, _XLSX_MIME, "contactos.xlsx")

    by_id = {c["context_id"]: c for c in summary["mapping_contexts"]}
    assert by_id["sheet:Clientes"]["entity_type"] == "customer"
    assert by_id["sheet:Proveedores"]["entity_type"] == "supplier"


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


# --- El desempate a "stock" no pisa la evidencia transaccional ---------------
#
# Headers reales del archivo del usuario. No se retocan (paréntesis, tildes,
# "N°" incluidos): el clasificador tiene que acertar sobre lo que la gente
# escribe, no sobre una versión prolija.

_HEADERS_VENTAS_REAL = [
    "Fecha",
    "Hora",
    "N° Comprobante",
    "Cliente (tal cual se anotó)",
    "Producto (tal cual se anotó)",
    "Cantidad",
    "Precio Unitario",
    "Total",
    "Medio de Pago",
]

_HEADERS_CLIENTES_REAL = [
    "ID",
    "Nombre (correcto)",
    "Documento",
    "Tipo",
    "Registrado",
    "Variantes de nombre vistas en ventas",
]


def test_hoja_de_ventas_real_se_clasifica_como_ventas() -> None:
    """Una exportación de ventas con columna de producto es una VENTA, no un catálogo.

    Trae las tres señales de operación juntas: fecha + monto de la operación
    ("Total", exacto en TRANSACCION_MONTO_COLS) + contexto de venta ("Cliente").
    El catch-all `if has_nombre: return "stock"` la pisaba igual — y ese return
    solo es alcanzable con nombre+venta+fecha, o sea exactamente sobre este caso.
    """
    assert analyze_headers(_HEADERS_VENTAS_REAL)["inferred_type"] == "ventas"


def test_hoja_de_clientes_real_se_clasifica_como_clientes() -> None:
    """Un maestro de clientes que identifica por "Documento" (no por "DNI").

    Sin `documento` en CLIENTE_SIGNAL_COLS, cliente_score quedaba en 0, la regla
    de maestros nunca se evaluaba y la hoja caía a stock por `has_nombre and not
    has_fecha`.
    """
    assert analyze_headers(_HEADERS_CLIENTES_REAL)["inferred_type"] == "clientes"


def test_hoja_de_clientes_gana_a_la_senal_de_venta_de_una_columna() -> None:
    """Trampa real: la columna "Variantes de nombre vistas en ventas" mete la
    palabra "ventas" en un maestro de clientes (venta_score=1, has_venta=True).

    La hoja sigue siendo un maestro porque la regla de maestros corre ANTES que
    las reglas de venta. Este test fija ese orden: si alguien mueve la regla
    -0.5 debajo del scoring, la hoja se convierte en ventas y el test cae.
    """
    analysis = analyze_headers(_HEADERS_CLIENTES_REAL)
    assert analysis["has_venta"] is True  # la columna inyecta la señal espuria
    assert analysis["inferred_type"] == "clientes"  # y aun así no decide


def test_xlsx_ventas_y_clientes_reales_van_a_sus_buckets() -> None:
    """End-to-end sobre el archivo real: ninguna de las dos hojas cae en stock."""
    content = _build_multisheet_xlsx(
        {
            "Ventas": [
                [*_HEADERS_VENTAS_REAL],
                [
                    "2026-05-01",
                    "14:30",
                    "0001-00012345",
                    "Juan Pérez",
                    "Coca-Cola 600ml",
                    "2",
                    "1200",
                    "2400",
                    "Efectivo",
                ],
            ],
            "Clientes": [
                [*_HEADERS_CLIENTES_REAL],
                ["1", "Juan Pérez", "30111222", "DNI", "2026-04-01", "juan perez; J. Perez"],
            ],
        }
    )
    summary = parse_uploaded_content(content, _XLSX_MIME, "negocio.xlsx")

    by_id = {c["context_id"]: c for c in summary["mapping_contexts"]}
    assert by_id["sheet:Ventas"]["entity_type"] == "sale"
    assert by_id["sheet:Clientes"]["entity_type"] == "customer"
    assert len(summary["ventas_detectadas"]) == 1
    assert len(summary["clientes_detectados"]) == 1
    assert not summary["stock_detectado"]


def test_lista_de_precios_sigue_siendo_stock() -> None:
    """No-regresión del cambio de arriba: sin fecha ni monto de operación, un
    catálogo sigue siendo catálogo. Es el caso que el desempate decía proteger."""
    assert (
        analyze_headers(["nombre", "precio_venta", "stock_actual", "stock_minimo"])[
            "inferred_type"
        ]
        == "stock"
    )
    assert analyze_headers(["sku", "producto", "precio", "stock"])["inferred_type"] == "stock"


def test_catalogo_con_fecha_de_alta_sigue_siendo_stock() -> None:
    """Un catálogo con fecha pero SIN monto de operación tampoco se escapa a ventas:
    falta la evidencia transaccional completa."""
    assert (
        analyze_headers(["fecha_alta", "producto", "precio_venta", "stock"])["inferred_type"]
        == "stock"
    )


def test_forma_de_pago_no_es_senal_de_venta() -> None:
    """Una columna de forma de pago NO discrimina venta de gasto: un libro de
    compras también la trae. Solo dice que hay una operación.

    Cuando contaba como señal de venta le empataba el score a una compra real
    (proveedor=1 vs forma_pago=1), la regla -1 no disparaba y el libro de compras
    se importaba como catálogo: se perdían el COGS y la salida de caja. Las tres
    grafías tienen que dar lo mismo — antes daban gastos/gastos/stock.
    """
    for pago in ("forma_pago", "forma_de_pago", "medio_de_pago"):
        headers = ["fecha", "proveedor", "producto", "cantidad", "total", pago]
        assert analyze_headers(headers)["inferred_type"] == "gastos", pago

    # Y sin contexto de venta real, la forma de pago sola no alcanza para "ventas".
    assert analyze_headers(["fecha", "monto", "medio_de_pago"])["inferred_type"] == "general"


def test_catalogo_con_fecha_y_total_no_se_convierte_en_ventas() -> None:
    """El agujero del guard nuevo: "precio_venta" activa `has_venta` por substring
    aunque sea un campo del catálogo, y "Total" (valuación del stock) es un monto
    exacto. Con una fecha de alta, una lista de precios juntaba las tres señales de
    operación y se importaba como ventas — inventando facturación en el score.

    El contexto tiene que venir de una columna que no sea de precio.
    """
    assert (
        analyze_headers(["fecha_alta", "producto", "precio_venta", "total"])["inferred_type"]
        == "stock"
    )
    assert (
        analyze_headers(["fecha", "nombre", "precio_venta", "total", "stock"])["inferred_type"]
        == "stock"
    )
    # Con costo Y precio (catálogo valuado) tampoco.
    assert (
        analyze_headers(["fecha", "producto", "precio_venta", "costo_unitario", "total"])[
            "inferred_type"
        ]
        == "stock"
    )


def test_medio_de_pago_desempata_un_libro_de_compras_con_sku() -> None:
    """El otro lado del mismo agujero: FORMA_PAGO_COLS matchea EXACTO.

    Con `sku` presente, la regla 1 devuelve "stock" antes de llegar a cualquier
    scoring, así que la regla -1 (libro de compras) es lo único que puede salvar
    la compra — y exige forma de pago o proveedor. Sin proveedor y con la forma
    de pago escrita "medio_de_pago", el set no matcheaba: la compra se importaba
    como catálogo y se perdían el COGS y la salida de caja.
    """
    compras = [
        "fecha",
        "sku",
        "producto",
        "cantidad",
        "total",
        "medio_de_pago",
        "categoria",
        "costo_unitario",
    ]
    assert analyze_headers(compras)["inferred_type"] == "gastos"
    # Misma hoja escrita sin la preposición: ya funcionaba, y tiene que seguir igual.
    sin_preposicion = [c.replace("medio_de_pago", "forma_pago") for c in compras]
    assert analyze_headers(sin_preposicion)["inferred_type"] == "gastos"


def test_maestro_de_clientes_con_fecha_de_nacimiento_sigue_siendo_clientes() -> None:
    """`fecha_nacimiento` es una señal de CLIENTE, no de operación.

    Pero `has_fecha` matchea el substring "fecha", así que activaba el guard
    `not has_fecha` de la regla de maestros y la mataba: un maestro de clientes
    con cumpleaños escrito "Fecha de nacimiento" no podía clasificar como
    clientes — aunque `fecha_nacimiento` esté listada en CLIENTE_SIGNAL_COLS.
    """
    assert (
        analyze_headers(["nombre", "dni", "email", "fecha_nacimiento"])["inferred_type"]
        == "clientes"
    )


def test_venta_con_fecha_real_no_se_confunde_con_maestro() -> None:
    """No-regresión del cambio de arriba: una fecha de operación sigue bloqueando
    la regla de maestros (si no, una venta con cliente caería en 'clientes')."""
    assert analyze_headers(["fecha", "cliente", "total", "producto"])["inferred_type"] == "ventas"
