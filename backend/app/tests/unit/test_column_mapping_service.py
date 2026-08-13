"""Tests for ColumnMappingService: sugerencias, aprendizaje y eliminación."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from app.application.services.column_mapping_service import (
    CANONICAL_FIELDS,
    REQUIRED_FIELDS,
    SINGLE_VALUE_FIELDS,
    ColumnMappingService,
    _heuristic_match,
    _match_key,
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


def test_resolve_date_column_mapeo_a_columna_inexistente_no_cuenta() -> None:
    # Un mapeo a una columna que NO está en los headers no satisface el gate: el
    # importador obtendría None por fila. Sin heurística que salve, devuelve None.
    assert (
        resolve_transaction_date_column(
            ["detalle", "monto"], {"col_inexistente": "transaction_date"}
        )
        is None
    )
    # Pero si además hay una columna de fecha real, la heurística la encuentra.
    assert (
        resolve_transaction_date_column(
            ["fecha", "monto"], {"col_inexistente": "transaction_date"}
        )
        == "fecha"
    )


def test_validate_required_date_mapping_reporta_solo_los_sin_fecha() -> None:
    included: list[tuple[str, list[str] | None, dict[str, str]]] = [
        ("Ventas", ["fecha", "monto"], {}),
        ("Gastos", ["detalle", "monto"], {}),  # sin fecha
    ]
    assert validate_required_date_mapping(included) == ["Gastos"]
    # Todos con fecha → lista vacía.
    ok: list[tuple[str, list[str] | None, dict[str, str]]] = [("Ventas", ["fecha", "monto"], {})]
    assert validate_required_date_mapping(ok) == []


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


# ── F7a: entidades customer/supplier (maestros) + campos de referencia ─────────


def test_heuristic_match_customer() -> None:
    assert _heuristic_match("nombre", "customer") == "name"
    assert _heuristic_match("apellido", "customer") == "last_name"
    assert _heuristic_match("dni", "customer") == "dni"
    assert _heuristic_match("cuit", "customer") == "cuit"
    assert _heuristic_match("email", "customer") == "email"
    assert _heuristic_match("telefono", "customer") == "phone"
    assert _heuristic_match("localidad", "customer") == "locality"


def test_heuristic_match_supplier() -> None:
    assert _heuristic_match("nombre", "supplier") == "name"
    assert _heuristic_match("cuil", "supplier") == "cuil"
    assert _heuristic_match("forma_pago", "supplier") == "payment_method"
    assert _heuristic_match("email", "supplier") == "email"
    assert _heuristic_match("telefono", "supplier") == "phone"


def test_heuristic_match_sale_customer_reference_fields() -> None:
    """Columnas de referencia al cliente en una hoja de VENTAS (para que 7c pueda
    mapearlas más adelante — esta PR solo abre el contrato)."""
    assert _heuristic_match("dni", "sale") == "customer_dni"
    assert _heuristic_match("cuit", "sale") == "customer_cuit"
    assert _heuristic_match("cliente", "sale") == "customer_name"
    # "nombre" a secas sigue siendo product_name — no lo pisa customer_name.
    assert _heuristic_match("nombre", "sale") == "product_name"


def test_heuristic_match_expense_supplier_reference_fields() -> None:
    """Columnas de referencia al proveedor en una hoja de GASTOS."""
    assert _heuristic_match("cuil", "expense") == "supplier_cuil"
    assert _heuristic_match("email", "expense") == "supplier_email"
    # supplier_name ya existía (Sprint 21) — sigue intacto.
    assert _heuristic_match("proveedor", "expense") == "supplier_name"


def test_required_fields_structure() -> None:
    assert "amount" in REQUIRED_FIELDS["sale"]
    assert "transaction_date" in REQUIRED_FIELDS["sale"]
    assert "amount" in REQUIRED_FIELDS["expense"]
    assert "expense_date" in REQUIRED_FIELDS["expense"]
    assert "name" in REQUIRED_FIELDS["product"]
    assert REQUIRED_FIELDS["customer"] == ["name"]
    assert REQUIRED_FIELDS["supplier"] == ["name"]


def test_canonical_fields_all_entities() -> None:
    assert "sale" in CANONICAL_FIELDS
    assert "expense" in CANONICAL_FIELDS
    assert "product" in CANONICAL_FIELDS
    assert "customer" in CANONICAL_FIELDS
    assert "supplier" in CANONICAL_FIELDS
    assert "amount" in CANONICAL_FIELDS["sale"]
    assert "name" in CANONICAL_FIELDS["product"]
    assert "dni" in CANONICAL_FIELDS["customer"]
    assert "cuit" in CANONICAL_FIELDS["customer"]
    # CUIL y CUIT conviven (mig `20260813_0001`): un proveedor persona física
    # tiene CUIL y una empresa CUIT, y la empresa es el caso mayoritario — hasta
    # esa migración el dato fiscal del proveedor típico no tenía dónde ir.
    assert "cuil" in CANONICAL_FIELDS["supplier"]
    assert "cuit" in CANONICAL_FIELDS["supplier"]
    # `iva_condition` entró con el CUIT, por el mismo motivo: un padrón de
    # proveedores trae «Condición IVA» y quedaba sin destino.
    assert "iva_condition" in CANONICAL_FIELDS["supplier"]
    # El domicilio sigue afuera: el modelo `Supplier` no lo persiste.
    assert "address" not in CANONICAL_FIELDS["supplier"]
    # Campos de referencia en las entidades transaccionales.
    assert "customer_dni" in CANONICAL_FIELDS["sale"]
    assert "supplier_cuil" in CANONICAL_FIELDS["expense"]


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


async def test_suggest_mappings_customer_master() -> None:
    """suggest_mappings resuelve columnas fiscales de un maestro de clientes."""
    db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=mock_result)

    svc = ColumnMappingService(db)
    headers = ["Nombre", "DNI", "CUIT", "Email"]
    sample_rows = [
        {"Nombre": "Juan Perez", "DNI": "30111222", "CUIT": "20301112223", "Email": "juan@mail.com"}
    ]

    suggestions = await svc.suggest_mappings(uuid.uuid4(), "customer", headers, sample_rows)

    by_col = {s["source_column"]: s for s in suggestions}
    assert by_col["Nombre"]["target_field"] == "name"
    assert by_col["DNI"]["target_field"] == "dni"
    assert by_col["CUIT"]["target_field"] == "cuit"
    assert by_col["Email"]["target_field"] == "email"


async def test_suggest_mappings_sale_with_customer_reference_column() -> None:
    """Una hoja de VENTAS con columna 'DNI' (referencia al cliente) la mapea a
    customer_dni — el contrato que habilita 7c, sin implementar la vinculación."""
    db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=mock_result)

    svc = ColumnMappingService(db)
    headers = ["Fecha", "Monto", "DNI"]
    sample_rows = [{"Fecha": "2024-01-15", "Monto": "1500", "DNI": "30111222"}]

    suggestions = await svc.suggest_mappings(uuid.uuid4(), "sale", headers, sample_rows)

    dni_sugg = next(s for s in suggestions if s["source_column"] == "DNI")
    assert dni_sugg["target_field"] == "customer_dni"
    assert dni_sugg["status"] == "mapped"


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


async def test_delete_mapping_returns_false_when_not_found() -> None:
    """delete_mapping retorna False si el mapping no pertenece al tenant."""
    db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=mock_result)

    svc = ColumnMappingService(db)
    result = await svc.delete_mapping(uuid.uuid4(), uuid.uuid4())
    assert result is False


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


# ── Los tres precios de un catálogo resuelven a campos DISTINTOS ───────────────
#
# Incidente ASTERIA (2026-07-31): "Precio de compra", "Precio de lista" y
# "Precio de venta final" caían los tres en `sale_price_ars`. El costo entraba
# como precio de venta y dos columnas se perdían en silencio.


def test_precio_de_compra_es_costo_no_precio_de_venta() -> None:
    """El caso que rompió: `precio` (6) y `compra` (6) empataban en longitud y
    ganaba el orden de declaración del dict."""
    assert _heuristic_match(_normalize_col("Precio de compra"), "product") == "unit_cost_ars"
    assert _heuristic_match(_normalize_col("Precio compra"), "product") == "unit_cost_ars"
    assert _heuristic_match(_normalize_col("precio unitario"), "product") == "unit_cost_ars"


def test_precio_de_lista_es_sugerido_no_precio_de_venta() -> None:
    assert _heuristic_match(_normalize_col("Precio de lista"), "product") == "list_price_ars"
    assert _heuristic_match(_normalize_col("Precio sugerido"), "product") == "list_price_ars"


def test_precio_de_venta_final_es_precio_de_venta() -> None:
    assert _heuristic_match(_normalize_col("Precio de venta final"), "product") == "sale_price_ars"
    assert _heuristic_match(_normalize_col("Precio de venta"), "product") == "sale_price_ars"


def test_las_tres_columnas_de_asteria_no_colisionan() -> None:
    """Las tres juntas, como venían en la hoja real: tres targets distintos."""
    headers = ["Productos", "Precio de compra", "Precio de lista", "Precio de venta final"]
    targets = [_heuristic_match(_normalize_col(h), "product") for h in headers]
    assert targets == ["name", "unit_cost_ars", "list_price_ars", "sale_price_ars"]
    assert len(set(targets)) == 4  # ninguno pisa a otro


def test_precio_unitario_en_ventas_es_el_precio_vendido() -> None:
    """El MISMO header significa otra cosa según la entidad: en un catálogo es el
    costo de compra, en una hoja de ventas es lo que se cobró."""
    assert _heuristic_match(_normalize_col("Precio unitario"), "sale") == "unit_price"
    assert _heuristic_match(_normalize_col("Precio unitario"), "product") == "unit_cost_ars"


def test_match_key_colapsa_preposiciones_sin_tocar_normalize_col() -> None:
    """`_normalize_col` NO puede cambiar: es lo que se persiste en
    `tenant_column_mappings.source_column`. Un alias ya aprendido por un tenant
    sigue matcheando con la misma forma normalizada de siempre."""
    assert _normalize_col("Precio de compra") == "precio_de_compra"
    assert _match_key("precio_de_compra") == "precio_compra"
    # Header que es solo stopwords: nunca clave vacía (matchearía cualquier cosa).
    assert _match_key("de") == "de"


def test_heuristicas_previas_no_regresionan() -> None:
    """Los headers comunes siguen resolviendo igual que antes del cambio."""
    assert _heuristic_match(_normalize_col("forma de pago"), "expense") == "payment_method"
    assert _heuristic_match(_normalize_col("forma_pago"), "expense") == "payment_method"
    assert _heuristic_match(_normalize_col("fecha"), "sale") == "transaction_date"
    assert _heuristic_match(_normalize_col("monto"), "sale") == "amount"
    assert _heuristic_match(_normalize_col("cantidad"), "sale") == "quantity"
    assert _heuristic_match(_normalize_col("Stock"), "product") == "stock_units"
    assert _heuristic_match(_normalize_col("Proveedor"), "expense") == "supplier_name"


# ── Campos de valor único: la colisión no se puede desempatar sola ─────────────


def test_single_value_fields_cubre_dinero_cantidad_y_fecha() -> None:
    assert "amount" in SINGLE_VALUE_FIELDS["sale"]
    assert "quantity" in SINGLE_VALUE_FIELDS["sale"]
    assert "transaction_date" in SINGLE_VALUE_FIELDS["sale"]
    assert "unit_price" in SINGLE_VALUE_FIELDS["sale"]
    assert "expense_date" in SINGLE_VALUE_FIELDS["expense"]
    assert SINGLE_VALUE_FIELDS["product"] == {
        "sale_price_ars",
        "list_price_ars",
        "unit_cost_ars",
        "stock_units",
    }
    # Campos donde varias columnas pueden ser legítimas: fuera del bloqueo.
    assert "notes" not in SINGLE_VALUE_FIELDS["sale"]
    assert "category" not in SINGLE_VALUE_FIELDS["expense"]


def test_todo_campo_escalar_existe_en_el_catalogo_canonico() -> None:
    """Un typo en SINGLE_VALUE_FIELDS bloquearía por un campo inexistente o, peor,
    dejaría de bloquear el que sí importa."""
    for entity, fields in SINGLE_VALUE_FIELDS.items():
        for f in fields:
            assert f in CANONICAL_FIELDS[entity], f"{entity}.{f} no está en CANONICAL_FIELDS"
