"""Unit tests para DataRepairService — sin DB real, mocks de SQLAlchemy."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from app.application.services.data_repair_service import (
    _extract_price_set,
    _extract_product_rows,
    _parse_quantity_from_notes,
    _re_evaluate_summary,
    normalize_product_name,
)

# ── normalize_product_name ────────────────────────────────────────────────────


def test_normalize_removes_hyphens_and_extra_spaces():
    assert normalize_product_name("Coca-Cola  600ml") == "coca cola 600ml"


def test_normalize_lowercases_and_strips():
    assert normalize_product_name("  AGUA Mineral  ") == "agua mineral"


def test_normalize_underscores_to_space():
    assert normalize_product_name("precio_venta") == "precio venta"


# ── _re_evaluate_summary ─────────────────────────────────────────────────────


def test_re_evaluate_product_csv_returns_stock():
    """Un summary de CSV de productos (nombre + precio) → stock con nueva lógica."""
    summary = {
        "has_fecha": True,
        "has_venta": False,
        "has_gasto": False,
        "has_producto": True,
        "columns": ["fecha", "nombre", "precio"],
        "inferred_type": "ventas",  # clasificación original (incorrecta)
    }
    result = _re_evaluate_summary(summary)
    assert result == "stock"


def test_re_evaluate_sale_csv_remains_ventas():
    """Un summary de CSV de ventas (fecha + monto sin nombre/sku) → ventas."""
    summary = {
        "has_fecha": True,
        "has_venta": True,
        "has_gasto": False,
        "has_producto": False,
        "columns": ["fecha", "monto", "descripcion"],
        "inferred_type": "ventas",
    }
    result = _re_evaluate_summary(summary)
    assert result == "ventas"


def test_re_evaluate_empty_summary_returns_none():
    assert _re_evaluate_summary({}) is None


def test_re_evaluate_no_marca_una_venta_legitima_como_stock():
    """Un archivo de ventas con columna de producto NO es un import mal clasificado.

    El detector re-evalúa con `infer_spreadsheet_type`, pero no le pasaba
    `has_monto_transaccion`, así que se quedaba con la lógica vieja: devolvía
    "stock" y el archivo entraba como candidato de
    `/admin/repairs/misclassified-product-imports` — o sea, candidato a que le
    anulen las ventas. Con la evidencia transaccional completa (fecha + total +
    cliente) la respuesta tiene que ser la misma que la del clasificador vivo.
    """
    summary = {
        "has_fecha": True,
        "has_venta": True,
        "has_gasto": False,
        "has_producto": True,
        "columns": [
            "fecha",
            "hora",
            "n°_comprobante",
            "cliente_(tal_cual_se_anotó)",
            "producto_(tal_cual_se_anotó)",
            "cantidad",
            "precio_unitario",
            "total",
            "medio_de_pago",
        ],
        "inferred_type": "ventas",
    }
    assert _re_evaluate_summary(summary) != "stock"


# ── _extract_product_rows ─────────────────────────────────────────────────────


def test_extract_product_rows_from_stock_detectado():
    summary = {
        "stock_detectado": [{"nombre": "Coca-Cola", "precio": "500"}],
        "ventas_detectadas": [{"monto": "9999"}],
    }
    rows = _extract_product_rows(summary)
    assert len(rows) == 1
    assert rows[0]["nombre"] == "Coca-Cola"


def test_extract_product_rows_fallback_to_preview():
    summary = {
        "preview_rows": [{"nombre": "Agua", "precio": "200"}],
    }
    rows = _extract_product_rows(summary)
    assert len(rows) == 1


# ── _extract_price_set ────────────────────────────────────────────────────────


def test_extract_price_set_from_precio_col():
    rows = [{"nombre": "Coca-Cola", "precio": "500"}, {"nombre": "Agua", "precio": "200"}]
    prices = _extract_price_set(rows)
    assert Decimal("500") in prices
    assert Decimal("200") in prices


def test_extract_price_set_empty_when_no_precio_col():
    rows = [{"nombre": "Producto", "stock": "10"}]
    prices = _extract_price_set(rows)
    assert prices == set()


def test_parse_quantity_from_chat_sale_notes():
    parsed = _parse_quantity_from_notes("venta de 3 coca colas")
    assert parsed == (3, "coca colas")


async def test_detect_chat_sale_quality_issue_unique_product():
    from app.application.services.data_repair_service import detect_chat_sale_quality_issues

    tenant_id = uuid.uuid4()
    product_id = uuid.uuid4()

    mock_sale = MagicMock()
    mock_sale.id = uuid.uuid4()
    mock_sale.tenant_id = tenant_id
    mock_sale.product_id = None
    mock_sale.notes = "venta de 3 coca colas"
    mock_sale.quantity = 1
    mock_sale.amount = Decimal("30000")

    mock_product = MagicMock()
    mock_product.id = product_id
    mock_product.tenant_id = tenant_id
    mock_product.name = "Coca-Cola 600ml"
    mock_product.sale_price_ars = Decimal("500")
    mock_product.is_active = True

    sales_result = MagicMock()
    sales_result.scalars.return_value.all.return_value = [mock_sale]
    product_result = MagicMock()
    product_result.scalars.return_value.all.return_value = [mock_product]
    empty_product_result = MagicMock()
    empty_product_result.scalars.return_value.all.return_value = []

    mock_db = _mock_db_session([sales_result, product_result, empty_product_result])
    candidates = await detect_chat_sale_quality_issues(mock_db, tenant_id=tenant_id)

    assert len(candidates) == 1
    assert candidates[0].confidence == "HIGH"
    assert candidates[0].quantity == 3
    assert candidates[0].product.id == product_id


# ── apply_repair dry_run ──────────────────────────────────────────────────────


def _mock_db_session(execute_side_effects: list[Any]) -> MagicMock:
    """Crea un mock de AsyncSession con begin_nested() correctamente mockeado."""
    mock_db = MagicMock()
    items_added: list[Any] = []
    mock_db.add = lambda item: items_added.append(item)
    mock_db.flush = AsyncMock()
    mock_db.get = AsyncMock(return_value=None)
    mock_db.execute = AsyncMock(side_effect=execute_side_effects)

    # begin_nested() debe ser awaitable y retornar algo con commit/rollback
    savepoint = MagicMock()
    savepoint.commit = AsyncMock()
    savepoint.rollback = AsyncMock()
    mock_db.begin_nested = AsyncMock(return_value=savepoint)

    return mock_db


async def test_dry_run_does_not_void_sales():
    """dry_run=True → DataRepairRun creado, SaleEntry.voided_at NO se toca."""
    from app.application.services.data_repair_service import apply_repair

    now = datetime.now(UTC)
    tenant_id = uuid.uuid4()
    file_id = uuid.uuid4()

    mock_file = MagicMock()
    mock_file.id = file_id
    mock_file.tenant_id = tenant_id
    mock_file.created_at = now
    mock_file.processing_status = "DONE"
    mock_file.parsed_summary_json = {
        "has_fecha": True,
        "has_venta": False,
        "has_gasto": False,
        "has_producto": True,
        "columns": ["fecha", "nombre", "precio"],
        "inferred_type": "ventas",
        "stock_detectado": [{"nombre": "Coca-Cola", "precio": "500"}],
        "ventas_detectadas": [{"nombre": "X", "monto": "500"}],
        "preview_rows": [{"nombre": "Coca-Cola", "precio": "500"}],
    }

    mock_sale = MagicMock()
    mock_sale.id = uuid.uuid4()
    mock_sale.amount = Decimal("500")
    mock_sale.transaction_date = now.date()
    mock_sale.notes = "Importado desde archivo"
    mock_sale.voided_at = None
    mock_sale.void_reason = None

    files_result = MagicMock()
    files_result.scalars.return_value.all.return_value = [mock_file]
    sales_result = MagicMock()
    sales_result.scalars.return_value.all.return_value = [mock_sale]
    # dry-run también hace lookup de producto para distinguir CREATE vs UPDATE
    product_lookup_result = MagicMock()
    product_lookup_result.scalar_one_or_none.return_value = None

    # B2: los detectores genéricos (duplicados + OPEX→COGS) corren después.
    # detect_chat_sale_quality_issues: 1 query (notes); duplicados: 2 queries
    # (ventas, gastos); _load_vertical_by_tenant: 1; misclassified: 1 (gastos).
    empty = MagicMock()
    empty.scalars.return_value.all.return_value = []
    empty_vertical = MagicMock()
    empty_vertical.all.return_value = []

    mock_db = _mock_db_session(
        [
            files_result,  # detect_misclassified: archivos
            sales_result,  # detect_misclassified: ventas del archivo
            empty,  # detect_chat: ventas con notes
            empty,  # detect_duplicates: ventas
            empty,  # detect_duplicates: gastos
            empty_vertical,  # _load_vertical_by_tenant
            empty,  # detect_misclassified_opex: gastos
            product_lookup_result,  # _process_candidate dry-run: lookup de producto
        ]
    )

    result = await apply_repair(mock_db, tenant_id=tenant_id, dry_run=True)

    assert result.dry_run is True
    assert result.candidates_found == 1
    assert result.sales_detected == 1
    assert result.sales_voided == 0
    assert mock_sale.voided_at is None


async def test_apply_voids_sales_and_creates_run():
    """dry_run=False → SaleEntry.voided_at se setea, run con COMPLETED."""
    from app.application.services.data_repair_service import apply_repair

    now = datetime.now(UTC)
    tenant_id = uuid.uuid4()
    file_id = uuid.uuid4()

    mock_file = MagicMock()
    mock_file.id = file_id
    mock_file.tenant_id = tenant_id
    mock_file.created_at = now
    mock_file.processing_status = "DONE"
    mock_file.parsed_summary_json = {
        "has_fecha": True,
        "has_venta": False,
        "has_gasto": False,
        "has_producto": True,
        "columns": ["fecha", "nombre", "precio"],
        "inferred_type": "ventas",
        "stock_detectado": [{"nombre": "Coca-Cola", "precio": "500"}],
        "ventas_detectadas": [{"monto": "500"}],
        "preview_rows": [{"nombre": "Coca-Cola", "precio": "500"}],
    }

    mock_sale = MagicMock()
    mock_sale.id = uuid.uuid4()
    mock_sale.amount = Decimal("500")
    mock_sale.transaction_date = now.date()
    mock_sale.notes = "Importado desde archivo"
    mock_sale.voided_at = None

    files_result = MagicMock()
    files_result.scalars.return_value.all.return_value = [mock_file]
    sales_result = MagicMock()
    sales_result.scalars.return_value.all.return_value = [mock_sale]

    with patch(
        "app.application.services.data_repair_service.insert_confirmed_data",
        AsyncMock(
            return_value={
                "ventas": 0,
                "gastos": 0,
                "productos": 1,
                "product_details": [
                    {
                        "action": "CREATED",
                        "product_id": str(uuid.uuid4()),
                        "name": "Coca-Cola",
                        "before": None,
                        "after": {"sale_price_ars": "500"},
                    }
                ],
            }
        ),
    ):
        empty = MagicMock()
        empty.scalars.return_value.all.return_value = []
        empty_vertical = MagicMock()
        empty_vertical.all.return_value = []
        mock_db = _mock_db_session(
            [
                files_result,  # detect_misclassified: archivos
                sales_result,  # detect_misclassified: ventas
                empty,  # detect_chat
                empty,  # detect_duplicates: ventas
                empty,  # detect_duplicates: gastos
                empty_vertical,  # _load_vertical_by_tenant
                empty,  # detect_misclassified_opex: gastos
            ]
        )
        result = await apply_repair(mock_db, tenant_id=tenant_id, dry_run=False)

    assert result.dry_run is False
    assert result.sales_voided == 1
    assert mock_sale.voided_at is not None
    assert mock_sale.void_reason == "REPAIR_MISCLASSIFIED_IMPORT"
    assert result.products_created == 1


async def test_no_touch_sale_with_product_id():
    """SaleEntry con product_id → no es candidata (ya tiene FK correcta)."""
    from app.application.services.data_repair_service import detect_misclassified_product_imports

    now = datetime.now(UTC)
    tenant_id = uuid.uuid4()

    mock_file = MagicMock()
    mock_file.id = uuid.uuid4()
    mock_file.tenant_id = tenant_id
    mock_file.created_at = now
    mock_file.processing_status = "DONE"
    mock_file.parsed_summary_json = {
        "has_fecha": True,
        "has_venta": False,
        "has_gasto": False,
        "has_producto": True,
        "columns": ["fecha", "nombre", "precio"],
        "inferred_type": "ventas",
        "stock_detectado": [{"nombre": "Coca-Cola", "precio": "500"}],
        "ventas_detectadas": [{"monto": "500"}],
        "preview_rows": [],
    }

    files_result = MagicMock()
    files_result.scalars.return_value.all.return_value = [mock_file]
    sales_result = MagicMock()
    sales_result.scalars.return_value.all.return_value = []
    mock_db = _mock_db_session([files_result, sales_result])

    candidates = await detect_misclassified_product_imports(mock_db, tenant_id=tenant_id)
    assert len(candidates) == 0


async def test_no_touch_non_import_sales():
    """CSV de ventas reales (fecha+monto+descripcion) → no genera candidatos."""
    from app.application.services.data_repair_service import detect_misclassified_product_imports

    now = datetime.now(UTC)
    tenant_id = uuid.uuid4()

    mock_file = MagicMock()
    mock_file.id = uuid.uuid4()
    mock_file.tenant_id = tenant_id
    mock_file.created_at = now
    mock_file.processing_status = "DONE"
    mock_file.parsed_summary_json = {
        "has_fecha": True,
        "has_venta": True,
        "has_gasto": False,
        "has_producto": False,
        "columns": ["fecha", "monto", "descripcion"],
        "inferred_type": "ventas",
        "ventas_detectadas": [{"monto": "5000"}],
    }

    files_result = MagicMock()
    files_result.scalars.return_value.all.return_value = [mock_file]
    mock_db = _mock_db_session([files_result])

    candidates = await detect_misclassified_product_imports(mock_db, tenant_id=tenant_id)
    assert len(candidates) == 0


async def test_dry_run_idempotent_already_voided():
    """SaleEntry con voided_at ya seteado no aparece en candidatos."""
    from app.application.services.data_repair_service import detect_misclassified_product_imports

    now = datetime.now(UTC)
    tenant_id = uuid.uuid4()

    mock_file = MagicMock()
    mock_file.id = uuid.uuid4()
    mock_file.tenant_id = tenant_id
    mock_file.created_at = now
    mock_file.processing_status = "DONE"
    mock_file.parsed_summary_json = {
        "has_fecha": True,
        "has_venta": False,
        "has_gasto": False,
        "has_producto": True,
        "columns": ["nombre", "precio"],
        "inferred_type": "ventas",
        "stock_detectado": [{"nombre": "Coca-Cola", "precio": "500"}],
        "ventas_detectadas": [{"monto": "500"}],
        "preview_rows": [],
    }

    files_result = MagicMock()
    files_result.scalars.return_value.all.return_value = [mock_file]
    sales_result = MagicMock()
    sales_result.scalars.return_value.all.return_value = []
    mock_db = _mock_db_session([files_result, sales_result])

    candidates = await detect_misclassified_product_imports(mock_db, tenant_id=tenant_id)
    assert len(candidates) == 0
