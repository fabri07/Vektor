"""FASE 3: clasificación contextual venta vs gasto.

Las columnas de dinero genéricas (monto/importe/total/precio) son NEUTRALES: no
deciden el tipo solas. El tipo se infiere por señales FUERTES de contexto
(cliente/ticket/cobro → venta; proveedor/categoría/concepto/servicio → gasto).
Ante empate o ausencia de contexto → 'general' (ambiguo): el usuario confirma,
nunca se importa como venta en silencio.
"""

from __future__ import annotations

from app.application.services.file_parsing import analyze_headers


def _t(headers: list[str]) -> str:
    return str(analyze_headers(headers)["inferred_type"])


# ── Deben ser GASTOS (antes caían como ventas por la columna de dinero) ───────


def test_alquiler_fecha_concepto_monto_is_gasto() -> None:
    assert _t(["fecha", "concepto", "monto"]) == "gastos"


def test_proveedor_concepto_importe_is_gasto() -> None:
    assert _t(["fecha", "proveedor", "concepto", "importe"]) == "gastos"


def test_categoria_descripcion_monto_is_gasto() -> None:
    assert _t(["fecha", "categoria", "descripcion", "monto"]) == "gastos"


def test_servicios_proveedor_monto_is_gasto() -> None:
    assert _t(["fecha", "servicio", "proveedor", "monto"]) == "gastos"


def test_impuesto_importe_is_gasto() -> None:
    assert _t(["fecha", "impuesto", "importe"]) == "gastos"


# ── Deben ser VENTAS (señal fuerte de venta) ──────────────────────────────────


def test_cliente_total_medio_pago_is_venta() -> None:
    assert _t(["fecha", "cliente", "total", "medio_pago"]) == "ventas"


def test_ticket_monto_is_venta() -> None:
    assert _t(["fecha", "ticket", "monto"]) == "ventas"


def test_facturacion_monto_is_venta() -> None:
    assert _t(["fecha", "facturacion", "monto"]) == "ventas"


# ── AMBIGUOS → general (no venta silenciosa) ──────────────────────────────────


def test_fecha_descripcion_monto_is_general() -> None:
    assert _t(["fecha", "descripcion", "monto"]) == "general"


def test_fecha_monto_only_is_general() -> None:
    assert _t(["fecha", "monto"]) == "general"


def test_money_columns_alone_do_not_force_ventas() -> None:
    # importe/total/precio sin contexto → ambiguo, no venta.
    assert _t(["fecha", "importe", "total"]) == "general"


# ── Empate de señales → general ───────────────────────────────────────────────


def test_conflicting_signals_tie_is_general() -> None:
    # cliente (venta) + proveedor (gasto), score 1 vs 1 → ambiguo.
    assert _t(["fecha", "cliente", "proveedor", "monto"]) == "general"


# ── Preservar lo ya hecho ─────────────────────────────────────────────────────


def test_merchandise_with_quantity_still_stock() -> None:
    assert _t(["fecha", "mercaderia", "cantidad", "costo"]) == "stock"


def test_compra_without_inventory_columns_not_stock() -> None:
    # "compra" + proveedor sin producto/cantidad → gasto, no stock.
    assert _t(["fecha", "compra", "proveedor", "monto"]) == "gastos"
