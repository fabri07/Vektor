"""FASE 1 (parser): detección de formato/encabezado, filas irregulares, sin truncamiento.

Cubre los bugs detectados en la auditoría:
  - CSV con `;` se leía como 1 columna (delimitador fijo a coma).
  - BOM UTF-8 contaminaba el primer header.
  - Fila de título arriba del encabezado → 0 filas detectadas.
  - CSV >50 filas se truncaba silenciosamente (`all_dicts[:50]`).
  - Filas con menos celdas que headers → columnas descartadas en silencio.
  - `ventas_detectadas` contaminado con filas de gastos (riesgo doble conteo).
"""

from __future__ import annotations

from app.application.services.file_parsing import (
    _decode_text_bytes,
    _detect_header_row,
    _sniff_delimiter,
    parse_uploaded_content,
    rows_to_dicts,
)

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# ── Helpers unitarios ─────────────────────────────────────────────────────────


def test_sniff_delimiter_detects_semicolon() -> None:
    assert _sniff_delimiter("fecha;monto;cliente\n2026-01-01;100;Juan") == ";"


def test_sniff_delimiter_detects_comma() -> None:
    assert _sniff_delimiter("fecha,monto,cliente\n2026-01-01,100,Juan") == ","


def test_sniff_delimiter_detects_tab() -> None:
    assert _sniff_delimiter("fecha\tmonto\tcliente\n2026-01-01\t100\tJuan") == "\t"


def test_decode_strips_utf8_bom() -> None:
    raw = "﻿precio_venta,monto\n100,200".encode()  # UTF-8 con BOM
    decoded = _decode_text_bytes(raw)
    assert not decoded.startswith("﻿")
    assert decoded.startswith("precio_venta")


def test_decode_handles_latin1() -> None:
    raw = "café,música\n1,2".encode("latin-1")
    decoded = _decode_text_bytes(raw)
    assert "caf" in decoded  # no crashea, decodifica algo razonable


def test_detect_header_row_skips_title_and_blank() -> None:
    rows = [
        ["Ventas Mensuales - Junio 2026"],  # título
        [],  # fila vacía
        ["fecha", "producto", "monto"],  # encabezado real
        ["2026-01-15", "Coca", "1500"],
    ]
    assert _detect_header_row(rows) == 2


def test_detect_header_row_defaults_to_zero_when_clean() -> None:
    rows = [["fecha", "monto"], ["2026-01-01", "100"]]
    assert _detect_header_row(rows) == 0


def test_rows_to_dicts_pads_ragged_rows() -> None:
    headers = ["fecha", "producto", "monto", "cliente"]
    rows = [["2026-01-16", "Pepsi"]]  # faltan monto y cliente
    result = rows_to_dicts(headers, rows)
    assert result == [
        {"fecha": "2026-01-16", "producto": "Pepsi", "monto": None, "cliente": None}
    ]


def test_rows_to_dicts_ignores_none_row_and_extra_cells() -> None:
    headers = ["a", "b"]
    rows = [None, [1, 2, 3, 4]]  # type: ignore[list-item]
    result = rows_to_dicts(headers, rows)
    assert result[0] == {"a": None, "b": None}
    assert result[1] == {"a": "1", "b": "2"}  # celdas extra ignoradas


# ── CSV end-to-end ────────────────────────────────────────────────────────────


def test_csv_semicolon_parsed_with_multiple_columns() -> None:
    csv = b"fecha;monto;descripcion\n2026-01-15;50000;Venta del dia\n2026-01-16;35000;Venta tarde"
    summary = parse_uploaded_content(csv, "text/csv", "ventas.csv")
    assert summary["columns"] == ["fecha", "monto", "descripcion"]
    assert summary["delimiter"] == ";"
    assert summary["row_count"] == 2


def test_csv_with_bom_clean_first_header() -> None:
    csv = "﻿fecha,monto\n2026-01-15,1000".encode()
    summary = parse_uploaded_content(csv, "text/csv", "ventas.csv")
    assert summary["columns"][0] == "fecha"  # sin


def test_csv_with_title_row_detects_real_headers() -> None:
    csv = (
        b"Reporte de Ventas - Junio\n"
        b"\n"
        b"fecha,monto,descripcion\n"
        b"2026-01-15,50000,Venta\n"
        b"2026-01-16,35000,Venta"
    )
    summary = parse_uploaded_content(csv, "text/csv", "ventas.csv")
    assert summary["columns"] == ["fecha", "monto", "descripcion"]
    assert summary["row_count"] == 2


def test_csv_over_50_rows_not_truncated() -> None:
    lines = ["fecha,monto"] + [f"2026-01-{(i % 28) + 1:02d},{1000 + i}" for i in range(200)]
    csv = "\n".join(lines).encode()
    summary = parse_uploaded_content(csv, "text/csv", "ventas.csv")
    assert summary["row_count"] == 200
    # Todas las filas quedan en el bucket (no truncadas a 50).
    assert len(summary["ventas_detectadas"]) == 200


def test_gastos_csv_does_not_contaminate_ventas_bucket() -> None:
    csv = b"fecha,gasto,proveedor\n2026-01-15,5000,ProvA\n2026-01-16,3000,ProvB"
    summary = parse_uploaded_content(csv, "text/csv", "gastos.csv")
    assert summary["inferred_type"] == "gastos"
    assert summary["gastos_detectados"]  # tiene gastos
    assert summary["ventas_detectadas"] == []  # NO contamina ventas


# ── Multi-hoja: preservación de hojas no clasificables ────────────────────────


def _build_multisheet_with_unclassified() -> bytes:
    import io

    import pytest

    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ventas = wb.active
    ventas.title = "Ventas"
    ventas.append(["fecha", "monto"])
    ventas.append(["2026-01-15", "50000"])
    resumen = wb.create_sheet("Resumen")  # nombre + headers no clasificables
    resumen.append(["Titulo", "Observaciones", "Estado"])
    resumen.append(["Total mes", "sin novedad", "ok"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_multisheet_unclassified_sheet_preserved_with_warning() -> None:
    content = _build_multisheet_with_unclassified()
    summary = parse_uploaded_content(content, _XLSX_MIME, "mixto.xlsx")
    contexts = summary["mapping_contexts"]
    by_label = {c["label"]: c for c in contexts}

    # La hoja clasificable entra normal.
    assert by_label["Ventas"]["entity_type"] == "sale"

    # La hoja "Resumen" NO se descarta: queda como contexto unclassified + warning.
    assert "Resumen" in by_label
    assert by_label["Resumen"]["entity_type"] is None
    assert by_label["Resumen"]["unclassified"] is True
    assert by_label["Resumen"]["row_count"] == 1
    assert any("Resumen" in w for w in summary["warnings"])
