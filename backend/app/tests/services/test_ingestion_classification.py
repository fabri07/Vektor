"""FASE 3: clasificación contextual venta vs gasto.

Las columnas de dinero genéricas (monto/importe/total/precio) son NEUTRALES: no
deciden el tipo solas. El tipo se infiere por señales FUERTES de contexto
(cliente/ticket/cobro → venta; proveedor/categoría/concepto/servicio → gasto).
Ante empate o ausencia de contexto → 'general' (ambiguo): el usuario confirma,
nunca se importa como venta en silencio.
"""

from __future__ import annotations

import pytest

from app.application.services.file_parsing import analyze_headers


def _t(headers: list[str]) -> str:
    return str(analyze_headers(headers)["inferred_type"])


@pytest.mark.parametrize(
    ("headers", "esperado"),
    [
        # ── Deben ser GASTOS (antes caían como ventas por la columna de dinero) ──
        pytest.param(
            ["fecha", "concepto", "monto"],
            "gastos",
            id="test_alquiler_fecha_concepto_monto_is_gasto",
        ),
        pytest.param(
            ["fecha", "proveedor", "concepto", "importe"],
            "gastos",
            id="test_proveedor_concepto_importe_is_gasto",
        ),
        pytest.param(
            ["fecha", "categoria", "descripcion", "monto"],
            "gastos",
            id="test_categoria_descripcion_monto_is_gasto",
        ),
        pytest.param(
            ["fecha", "servicio", "proveedor", "monto"],
            "gastos",
            id="test_servicios_proveedor_monto_is_gasto",
        ),
        pytest.param(
            ["fecha", "impuesto", "importe"],
            "gastos",
            id="test_impuesto_importe_is_gasto",
        ),
        # ── Deben ser VENTAS (señal fuerte de venta) ─────────────────────────────
        pytest.param(
            ["fecha", "cliente", "total", "medio_pago"],
            "ventas",
            id="test_cliente_total_medio_pago_is_venta",
        ),
        pytest.param(
            ["fecha", "ticket", "monto"],
            "ventas",
            id="test_ticket_monto_is_venta",
        ),
        pytest.param(
            ["fecha", "facturacion", "monto"],
            "ventas",
            id="test_facturacion_monto_is_venta",
        ),
        # ── AMBIGUOS → general (no venta silenciosa) ─────────────────────────────
        pytest.param(
            ["fecha", "descripcion", "monto"],
            "general",
            id="test_fecha_descripcion_monto_is_general",
        ),
        pytest.param(
            ["fecha", "monto"],
            "general",
            id="test_fecha_monto_only_is_general",
        ),
        # importe/total/precio sin contexto → ambiguo, no venta.
        pytest.param(
            ["fecha", "importe", "total"],
            "general",
            id="test_money_columns_alone_do_not_force_ventas",
        ),
        # ── Empate de señales → general ──────────────────────────────────────────
        # cliente (venta) + proveedor (gasto), score 1 vs 1 → ambiguo.
        pytest.param(
            ["fecha", "cliente", "proveedor", "monto"],
            "general",
            id="test_conflicting_signals_tie_is_general",
        ),
        # ── Preservar lo ya hecho ────────────────────────────────────────────────
        pytest.param(
            ["fecha", "mercaderia", "cantidad", "costo"],
            "stock",
            id="test_merchandise_with_quantity_still_stock",
        ),
        # "compra" + proveedor sin producto/cantidad → gasto, no stock.
        pytest.param(
            ["fecha", "compra", "proveedor", "monto"],
            "gastos",
            id="test_compra_without_inventory_columns_not_stock",
        ),
    ],
)
def test_headers_se_clasifican_por_contexto(headers: list[str], esperado: str) -> None:
    assert _t(headers) == esperado
