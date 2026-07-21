"""Tests for ColumnMappingService: sugerencias, aprendizaje y eliminación."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.application.services.column_mapping_service import (
    CANONICAL_FIELDS,
    REQUIRED_FIELDS,
    ColumnMappingService,
    _heuristic_match,
    _normalize_col,
    resolve_transaction_date_column,
    validate_required_date_mapping,
)


def test_normalize_col() -> None:
    assert _normalize_col("Precio Unitario") == "precio_unitario"
    assert _normalize_col("  fecha  ") == "fecha"
    assert _normalize_col("P. Venta") == "p._venta"
    assert _normalize_col("MONTO") == "monto"


# ── F6-A1: resolver de columna de fecha (fuente única API + importador) ─────────


def test_resolve_date_column_por_header_heuristico() -> None:
    assert resolve_transaction_date_column(["Fecha", "Monto"], None) == "Fecha"
    assert resolve_transaction_date_column(["dia", "importe"], None) == "dia"
    # Sin ninguna columna de fecha → None (dispara el 422 del gate).
    assert resolve_transaction_date_column(["detalle", "monto"], None) is None
    assert resolve_transaction_date_column(None, None) is None


def test_resolve_date_column_mapeo_explicito_gana() -> None:
    # El mapeo explícito precede a la heurística: "columna_x" mapeada a
    # transaction_date resuelve aunque no matchee ningún keyword.
    assert (
        resolve_transaction_date_column(
            ["columna_x", "monto"], {"columna_x": "transaction_date"}
        )
        == "columna_x"
    )
    assert (
        resolve_transaction_date_column(["col", "monto"], {"col": "expense_date"})
        == "col"
    )
    # Mapeo que NO es de fecha → cae a la heurística (acá tampoco hay) → None.
    assert (
        resolve_transaction_date_column(["detalle", "monto"], {"detalle": "notes"})
        is None
    )


def test_validate_required_date_mapping_reporta_solo_los_sin_fecha() -> None:
    included = [
        ("Ventas", ["fecha", "monto"], {}),
        ("Gastos", ["detalle", "monto"], {}),  # sin fecha
    ]
    assert validate_required_date_mapping(included) == ["Gastos"]
    # Todos con fecha → lista vacía.
    assert validate_required_date_mapping([("Ventas", ["fecha", "monto"], {})]) == []


def test_heuristic_match_sale() -> None:
    assert _heuristic_match("monto", "sale") == "amount"
    assert _heuristic_match("fecha", "sale") == "transaction_date"
    assert _heuristic_match("precio_venta", "sale") == "amount"
    assert _heuristic_match("cantidad", "sale") == "quantity"
    assert _heuristic_match("producto", "sale") == "product_name"
    assert _heuristic_match("observaciones", "sale") == "notes"


def test_heuristic_match_expense() -> None:
    assert _heuristic_match("gasto", "expense") == "amount"
    assert _heuristic_match("fecha", "expense") == "expense_date"
    assert _heuristic_match("categoria", "expense") == "category"
    assert _heuristic_match("proveedor", "expense") == "supplier_name"


def test_heuristic_match_expense_payment_method_not_amount() -> None:
    """Regresión: `forma_pago` debe ir a payment_method, no a amount por el
    substring 'pago' (el match exacto / keyword más largo gana)."""
    assert _heuristic_match("forma_pago", "expense") == "payment_method"
    assert _heuristic_match("forma_de_pago", "expense") == "payment_method"
    assert _heuristic_match("metodo_pago", "expense") == "payment_method"
    assert _heuristic_match("medio_de_pago", "expense") == "payment_method"
    # 'pago' a secas sigue siendo monto (pagos = egresos)
    assert _heuristic_match("pago", "expense") == "amount"


def test_heuristic_match_expense_is_recurring() -> None:
    assert _heuristic_match("recurrente", "expense") == "is_recurring"
    assert _heuristic_match("recurring", "expense") == "is_recurring"


def test_heuristic_match_product() -> None:
    assert _heuristic_match("sku", "product") == "sku"
    assert _heuristic_match("nombre", "product") == "name"
    assert _heuristic_match("precio_venta", "product") == "sale_price_ars"
    assert _heuristic_match("costo", "product") == "unit_cost_ars"
    assert _heuristic_match("stock", "product") == "stock_units"


def test_heuristic_match_product_barcode() -> None:
    # Review F2 #4: barcode mapeable. El header llega YA normalizado (underscores)
    # del caller (_normalize_col). "codigo_de_barras" matchea exacto → barcode;
    # "codigo" a secas sigue siendo sku.
    assert _heuristic_match("ean", "product") == "barcode"
    assert _heuristic_match("codigo_de_barras", "product") == "barcode"
    assert _heuristic_match("upc", "product") == "barcode"
    assert _heuristic_match("barras", "product") == "barcode"
    assert _heuristic_match("codigo", "product") == "sku"


def test_heuristic_match_none_for_unknown() -> None:
    assert _heuristic_match("xyz_desconocido_123", "sale") is None
    assert _heuristic_match("color", "product") is None


def test_required_fields_structure() -> None:
    assert "amount" in REQUIRED_FIELDS["sale"]
    assert "transaction_date" in REQUIRED_FIELDS["sale"]
    assert "amount" in REQUIRED_FIELDS["expense"]
    assert "expense_date" in REQUIRED_FIELDS["expense"]
    assert "name" in REQUIRED_FIELDS["product"]


def test_canonical_fields_all_entities() -> None:
    assert "sale" in CANONICAL_FIELDS
    assert "expense" in CANONICAL_FIELDS
    assert "product" in CANONICAL_FIELDS
    assert "amount" in CANONICAL_FIELDS["sale"]
    assert "name" in CANONICAL_FIELDS["product"]


@pytest.mark.asyncio
async def test_suggest_mappings_heuristic() -> None:
    """suggest_mappings usa heurística cuando no hay historial."""
    db = AsyncMock()
    # Simular historial vacío
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=mock_result)

    svc = ColumnMappingService(db)
    headers = ["Fecha", "Monto", "Producto"]
    sample_rows = [{"Fecha": "2024-01-15", "Monto": "1500", "Producto": "Coca Cola"}]

    suggestions = await svc.suggest_mappings(uuid.uuid4(), "sale", headers, sample_rows)

    assert len(suggestions) == 3
    fecha_sugg = next(s for s in suggestions if s["source_column"] == "Fecha")
    assert fecha_sugg["target_field"] == "transaction_date"
    assert fecha_sugg["source"] == "heuristic"
    assert fecha_sugg["status"] == "mapped"

    monto_sugg = next(s for s in suggestions if s["source_column"] == "Monto")
    assert monto_sugg["target_field"] == "amount"

    prod_sugg = next(s for s in suggestions if s["source_column"] == "Producto")
    assert prod_sugg["target_field"] == "product_name"


@pytest.mark.asyncio
async def test_suggest_mappings_tenant_history_priority() -> None:
    """Historial del tenant tiene prioridad sobre heurística."""
    db = AsyncMock()

    # Simular un mapeo previo: "P. Unitario" → "amount"
    mock_mapping = MagicMock()
    mock_mapping.source_column = "p._unitario"
    mock_mapping.target_field = "amount"
    mock_mapping.confirmed_count = 5

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [mock_mapping]
    db.execute = AsyncMock(return_value=mock_result)

    svc = ColumnMappingService(db)
    headers = ["P. Unitario"]
    sample_rows = [{"P. Unitario": "$1200"}]

    suggestions = await svc.suggest_mappings(uuid.uuid4(), "sale", headers, sample_rows)

    assert len(suggestions) == 1
    s = suggestions[0]
    assert s["source_column"] == "P. Unitario"
    assert s["target_field"] == "amount"
    assert s["source"] == "tenant_history"
    # Confianza crece con confirmed_count
    assert s["confidence"] > 0.5


@pytest.mark.asyncio
async def test_suggest_mappings_unknown_header() -> None:
    """Header desconocido sin fuzzy match → status unmapped."""
    db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=mock_result)

    svc = ColumnMappingService(db)
    headers = ["codigo_interno_xz99"]
    sample_rows = [{"codigo_interno_xz99": "abc"}]

    suggestions = await svc.suggest_mappings(uuid.uuid4(), "sale", headers, sample_rows)

    assert len(suggestions) == 1
    s = suggestions[0]
    assert s["status"] == "unmapped"


@pytest.mark.asyncio
async def test_suggest_mappings_sample_values() -> None:
    """sample_values toma hasta 5 valores no-nulos."""
    db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=mock_result)

    svc = ColumnMappingService(db)
    headers = ["Monto"]
    sample_rows: list[dict[str, Any]] = [
        {"Monto": "100"}, {"Monto": None}, {"Monto": "200"},
        {"Monto": "300"}, {"Monto": "400"}, {"Monto": "500"}, {"Monto": "600"},
    ]

    suggestions = await svc.suggest_mappings(uuid.uuid4(), "sale", headers, sample_rows)
    s = suggestions[0]
    # Máximo 5 valores no-nulos
    assert len(s["sample_values"]) <= 5
    assert all(v is not None for v in s["sample_values"])


@pytest.mark.asyncio
async def test_save_mappings_skip_ignore() -> None:
    """save_mappings no persiste mappings de tipo 'ignore'."""
    db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=mock_result)
    db.add = MagicMock()
    db.flush = AsyncMock()

    svc = ColumnMappingService(db)
    await svc.save_mappings(
        uuid.uuid4(),
        "sale",
        [
            {"source_column": "Fecha", "target_field": "transaction_date"},
            {"source_column": "Obs", "target_field": "ignore"},
        ],
    )

    # Solo se agrega 1 (la de "ignore" se omite)
    assert db.add.call_count == 1
    added = db.add.call_args[0][0]
    assert added.target_field == "transaction_date"


@pytest.mark.asyncio
async def test_save_mappings_increments_count_on_same_target() -> None:
    """Si el mapeo ya existe y el target es igual, incrementa confirmed_count."""
    db = AsyncMock()

    existing = MagicMock()
    existing.target_field = "amount"
    existing.confirmed_count = 3
    existing.last_seen_at = datetime.now(tz=UTC)

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = existing
    db.execute = AsyncMock(return_value=mock_result)
    db.flush = AsyncMock()

    svc = ColumnMappingService(db)
    await svc.save_mappings(
        uuid.uuid4(), "sale", [{"source_column": "Monto", "target_field": "amount"}]
    )

    assert existing.confirmed_count == 4


@pytest.mark.asyncio
async def test_save_mappings_resets_count_on_changed_target() -> None:
    """Si el usuario cambió el target, confirmed_count se reinicia a 1."""
    db = AsyncMock()

    existing = MagicMock()
    existing.target_field = "notes"  # target anterior
    existing.confirmed_count = 7
    existing.last_seen_at = datetime.now(tz=UTC)

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = existing
    db.execute = AsyncMock(return_value=mock_result)
    db.flush = AsyncMock()

    svc = ColumnMappingService(db)
    await svc.save_mappings(
        uuid.uuid4(), "sale", [{"source_column": "Observaciones", "target_field": "product_name"}]
    )

    assert existing.target_field == "product_name"
    assert existing.confirmed_count == 1


@pytest.mark.asyncio
async def test_delete_mapping_returns_false_when_not_found() -> None:
    """delete_mapping retorna False si el mapping no pertenece al tenant."""
    db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=mock_result)

    svc = ColumnMappingService(db)
    result = await svc.delete_mapping(uuid.uuid4(), uuid.uuid4())
    assert result is False


@pytest.mark.asyncio
async def test_delete_mapping_returns_true_when_found() -> None:
    """delete_mapping retorna True y borra el objeto cuando existe."""
    db = AsyncMock()

    existing = MagicMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = existing
    db.execute = AsyncMock(return_value=mock_result)
    db.delete = AsyncMock()
    db.flush = AsyncMock()

    svc = ColumnMappingService(db)
    result = await svc.delete_mapping(uuid.uuid4(), uuid.uuid4())
    assert result is True
    db.delete.assert_awaited_once_with(existing)
