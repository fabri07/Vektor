"""Libro Diario (doble encabezado Dinero/Mercadería × Ingreso/Egreso) + canónicos.

Cubre la remediación de la cuenta ASTERIA (decoración hogar):
  - El parser viejo no entendía el encabezado de dos filas → 0 ventas importadas.
  - Compras de mercadería deben entrar como gasto INVENTORY (COGS), no perderse.
  - Filas que continúan la fecha de la anterior (forward-fill).
  - Hoja "Ganancias" (derivada del libro) no se importa además → duplicaría.
  - payment_method canónico (mercadopago→qr, cuenta_corriente→account) y
    categorías de gasto que matchean el catálogo de productos del vertical.
"""

from __future__ import annotations

import io
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.cash_service import normalize_payment_method
from app.application.services.file_parsing import (
    detect_libro_diario_header,
    parse_libro_diario,
    parse_uploaded_content,
)
from app.application.services.ingestion_import_service import insert_confirmed_data
from app.domain.expense_categories import classify_expense_with_vertical
from app.domain.verticals import Vertical
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# ── Helpers ───────────────────────────────────────────────────────────────────

_LD_ROWS: list[list[Any]] = [
    # Encabezado de dos filas: grupos en celdas combinadas (None = no-ancla).
    ["Fecha", "Detalle", "Dinero", None, "Mercadería", None],
    [None, None, "Ingreso", "Egreso", "Ingreso", "Egreso"],
    # Venta: entra plata, sale mercadería (valorizada).
    ["2026-05-01", "Venta del día", 120000, None, None, 80000],
    # Compra de mercadería al contado: entra stock, sale plata (fecha ffill).
    [None, "Compra proveedor textiles", None, 50000, 50000, None],
    # Gasto operativo: solo sale plata.
    [None, "Pago luz", None, 15000, None, None],
    # Compra de mercadería fiada: entra stock SIN salida de plata → cta. cte.
    ["2026-05-02", "Compra fiada almacén", None, None, 30000, None],
    # Ambiguo: plata entra y sale en la misma fila → revisión manual.
    [None, "Movimiento raro", 5000, 5000, None, None],
    # Subtotal del libro: no es un movimiento.
    [None, "Total mes", 175000, None, None, None],
]


def _build_libro_diario_xlsx(*, with_ganancias: bool = False) -> bytes:
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ld = wb.active
    ld.title = "Libro Diario 2026"
    for row in _LD_ROWS:
        ld.append(row)
    if with_ganancias:
        gan = wb.create_sheet("Ganancias")
        gan.append(["fecha", "ingreso", "monto"])
        gan.append(["2026-05-01", "120000", "120000"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ── Detección del doble encabezado ────────────────────────────────────────────


def test_detect_libro_diario_header_with_merged_groups() -> None:
    detected = detect_libro_diario_header(_LD_ROWS)
    assert detected is not None
    hdr_idx, col_map = detected
    assert hdr_idx == 0
    assert col_map["fecha"] == 0
    assert col_map["detalle"] == 1
    assert col_map["dinero_ingreso"] == 2
    assert col_map["dinero_egreso"] == 3
    assert col_map["mercaderia_ingreso"] == 4
    assert col_map["mercaderia_egreso"] == 5


def test_detect_libro_diario_ignores_normal_files() -> None:
    assert detect_libro_diario_header([["fecha", "monto"], ["2026-01-01", "100"]]) is None
    # "Dinero" suelto sin sub-fila Ingreso/Egreso no alcanza.
    assert (
        detect_libro_diario_header(
            [["fecha", "dinero"], ["2026-01-01", "100"], ["2026-01-02", "200"]]
        )
        is None
    )


# ── Clasificación contable de filas ───────────────────────────────────────────


def test_parse_libro_diario_classifies_rows() -> None:
    detected = detect_libro_diario_header(_LD_ROWS)
    assert detected is not None
    parsed = parse_libro_diario(_LD_ROWS, *detected)

    assert len(parsed["ventas"]) == 1
    venta = parsed["ventas"][0]
    assert venta["monto"] == "120000"
    assert venta["fecha"] == "2026-05-01"

    assert len(parsed["gastos"]) == 3
    compra, luz, fiada = parsed["gastos"]
    # Compra al contado: el monto es la plata que salió; categoría mercadería.
    assert compra["monto"] == "50000"
    assert compra["categoria"] == "Mercadería"
    assert compra["fecha"] == "2026-05-01"  # forward-fill de la fila anterior
    assert "forma_pago" not in compra
    # Gasto operativo: la categoría se infiere del detalle.
    assert luz["monto"] == "15000"
    assert luz["categoria"] == "Pago luz"
    # Compra fiada: sin salida de plata → cuenta corriente del proveedor.
    assert fiada["monto"] == "30000"
    assert fiada["categoria"] == "Mercadería"
    assert fiada["forma_pago"] == "cuenta corriente"
    assert fiada["fecha"] == "2026-05-02"

    # Ambiguo → otros; subtotal ("Total mes") → descartado.
    assert len(parsed["otros"]) == 1
    assert parsed["otros"][0]["detalle"] == "Movimiento raro"


# ── Integración con _parse_spreadsheet ────────────────────────────────────────


def test_xlsx_libro_diario_single_sheet_summary() -> None:
    content = _build_libro_diario_xlsx()
    summary = parse_uploaded_content(content, _XLSX_MIME, "asteria.xlsx")

    assert summary["libro_diario"] is True
    assert summary["inferred_type"] == "mixed"
    assert summary["has_venta"] is True
    assert summary["has_gasto"] is True
    assert len(summary["ventas_detectadas"]) == 1
    assert len(summary["gastos_detectados"]) == 3
    assert len(summary["otros_detectados"]) == 1

    by_entity = {c["entity_type"]: c for c in summary["mapping_contexts"]}
    assert by_entity["sale"]["row_count"] == 1
    assert by_entity["expense"]["row_count"] == 3
    assert by_entity[None]["unclassified"] is True
    assert any("ambiguo" in w for w in summary["warnings"])


def test_xlsx_ganancias_sheet_excluded_when_libro_diario_present() -> None:
    content = _build_libro_diario_xlsx(with_ganancias=True)
    summary = parse_uploaded_content(content, _XLSX_MIME, "asteria.xlsx")

    by_label = {c["label"]: c for c in summary["mapping_contexts"]}
    # La hoja derivada queda sin clasificar (importarla duplicaría las ventas).
    assert by_label["Ganancias"]["entity_type"] is None
    assert by_label["Ganancias"]["unclassified"] is True
    assert any("Ganancias" in w and "duplicar" in w for w in summary["warnings"])
    # El libro fuente sí se clasifica.
    assert len(summary["ventas_detectadas"]) == 1


def test_csv_libro_diario_detected() -> None:
    csv_text = (
        "Fecha;Detalle;Dinero;;Mercaderia;\n"
        ";;Ingreso;Egreso;Ingreso;Egreso\n"
        "01/05/2026;Venta mostrador;15000;;;9000\n"
        ";Compra gaseosas;;8000;8000;\n"
    )
    summary = parse_uploaded_content(csv_text.encode(), "text/csv", "libro.csv")
    assert summary.get("libro_diario") is True
    assert len(summary["ventas_detectadas"]) == 1
    assert summary["ventas_detectadas"][0]["monto"] == "15000"
    assert len(summary["gastos_detectados"]) == 1
    assert summary["gastos_detectados"][0]["categoria"] == "Mercadería"


async def test_libro_diario_insert_end_to_end(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Parseo + inserción: venta, compra INVENTORY/COGS, OPEX y cta. cte."""
    content = _build_libro_diario_xlsx()
    summary = parse_uploaded_content(content, _XLSX_MIME, "asteria.xlsx")

    counts = await insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        {"ventas": True, "gastos": True},
    )
    assert counts["ventas"] == 1
    assert counts["gastos"] == 3
    assert counts["otros"] == 1  # el movimiento ambiguo va a la bandeja "Otros"

    sale = (await db_session.execute(select(SaleEntry))).scalar_one()
    assert sale.amount == Decimal("120000")
    assert sale.payment_method == "cash"

    expenses = (
        (await db_session.execute(select(ExpenseEntry).order_by(ExpenseEntry.amount)))
        .scalars()
        .all()
    )
    by_desc = {e.description: e for e in expenses}
    compra = by_desc["Compra proveedor textiles"]
    assert compra.category == "INVENTORY"
    assert compra.expense_type == "COGS"
    luz = by_desc["Pago luz"]
    assert luz.category == "UTILITIES"  # "luz" matchea el alias del catálogo
    assert luz.expense_type == "OPEX"
    fiada = by_desc["Compra fiada almacén"]
    assert fiada.category == "INVENTORY"
    assert fiada.expense_type == "COGS"
    assert fiada.payment_method == "account"  # "cuenta corriente" → canónico


# ── payment_method canónico ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("efectivo", "cash"),
        ("Efectivo", "cash"),
        ("transferencia", "transfer"),
        ("débito automático", "transfer"),
        ("debito", "debit_card"),
        ("debit_card", "debit_card"),
        ("credito", "credit_card"),
        ("mercadopago", "qr"),
        ("Mercado Pago", "qr"),
        ("MP", "qr"),
        ("qr", "qr"),
        ("cuenta_corriente", "account"),
        ("Cuenta Corriente", "account"),
        ("cta. cte.", "account"),
        ("fiado", "account"),
        ("cheque", "other"),
        (None, "other"),
        ("", "other"),
    ],
)
def test_normalize_payment_method(raw: str | None, expected: str) -> None:
    assert normalize_payment_method(raw) == expected


# ── Categorías de gasto que son mercadería del vertical ───────────────────────


@pytest.mark.parametrize(
    ("raw", "vertical", "code", "is_merch"),
    [
        ("Bebidas", Vertical.KIOSCO_ALMACEN, "INVENTORY", True),
        ("Golosinas", Vertical.KIOSCO_ALMACEN, "INVENTORY", True),
        ("Cigarrillos", Vertical.KIOSCO_ALMACEN, "INVENTORY", True),
        ("Textiles", Vertical.DECORACION_HOGAR, "INVENTORY", True),
        ("Mercadería", Vertical.KIOSCO_ALMACEN, "INVENTORY", True),
        ("Alquiler", Vertical.KIOSCO_ALMACEN, "RENT", False),
        ("Sueldos", Vertical.DECORACION_HOGAR, "PAYROLL", False),
    ],
)
def test_classify_expense_with_vertical_merchandise(
    raw: str, vertical: Vertical, code: str, is_merch: bool
) -> None:
    got_code, _label, got_merch = classify_expense_with_vertical(raw, vertical)
    assert got_code == code
    assert got_merch is is_merch


def test_classify_expense_with_vertical_preserves_label() -> None:
    # Categoría de producto → INVENTORY preservando el texto original.
    code, label, merch = classify_expense_with_vertical("Bebidas", Vertical.KIOSCO_ALMACEN)
    assert (code, label, merch) == ("INVENTORY", "Bebidas", True)
    # Sin match en ningún catálogo → OTHER con label (comportamiento previo).
    code, label, merch = classify_expense_with_vertical(
        "Gasto rarísimo xyz", Vertical.KIOSCO_ALMACEN
    )
    assert code == "OTHER"
    assert label == "Gasto rarísimo xyz"
    assert merch is False
