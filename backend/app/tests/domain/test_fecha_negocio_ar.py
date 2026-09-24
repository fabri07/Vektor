"""La frontera del "día" de una transacción es el día ARGENTINO, no el del server.

El server de producción corre en UTC (Railway), 3 horas ADELANTE de Argentina.
Entre las 21:00 y la medianoche AR, `date.today()` en el server ya devuelve el
día siguiente — así que el validador anti-fecha-futura aceptaba una venta
fechada MAÑANA en AR. Es permisivo de más, no restrictivo de más: deja entrar
justo lo que `MOTIVO_FECHA_FUTURA` existe para impedir (incidente ASTERIA
2026-09-14, 7 ventas reales fechadas en un año que todavía no había llegado).

Una caja vende hasta que cierra el local, así que produce transacciones en esa
franja todas las noches.
"""

from __future__ import annotations

import unittest.mock
from datetime import date, datetime, timedelta

import pytest

from app.schemas.purchase import ManualPurchaseRequest
from app.schemas.transaction import (
    BulkSaleRequest,
    CreateExpenseRequest,
    CreateSaleRequest,
    ManualBatchSaleRequest,
)

# Un día AR fijo y distinto de cualquier `date.today()` real: si el validador
# siguiera mirando el reloj del server, compararía contra otra fecha y estos
# casos romperían.
_DIA_AR = date(2026, 3, 15)
_HOY_2350 = datetime(2026, 3, 15, 23, 50)
_MANANA = datetime(2026, 3, 16, 9, 0)

_LINEA_DE_COMPRA = {
    "name": "Caja de fernet",
    "quantity": 1,
    "unit_cost": "9000.00",
    "sale_price_ars": "12000.00",
}


@pytest.fixture
def dia_ar_fijo():
    with unittest.mock.patch("app.schemas.transaction.today_ar", return_value=_DIA_AR):
        yield


@pytest.mark.usefixtures("dia_ar_fijo")
def test_una_venta_de_las_2350_ar_no_es_futura() -> None:
    """El caso que la caja produce todas las noches."""
    venta = CreateSaleRequest(
        amount="1500.00", transaction_date=_HOY_2350, payment_method="cash"
    )
    assert venta.transaction_date == _HOY_2350


@pytest.mark.usefixtures("dia_ar_fijo")
def test_una_venta_fechada_manana_en_ar_se_rechaza() -> None:
    """Con `date.today()` del server en UTC, a las 23:50 AR esto ENTRABA."""
    with pytest.raises(ValueError, match="future"):
        CreateSaleRequest(amount="1500.00", transaction_date=_MANANA, payment_method="cash")


@pytest.mark.usefixtures("dia_ar_fijo")
def test_manual_batch_tambien_usa_el_dia_ar() -> None:
    with pytest.raises(ValueError, match="future"):
        ManualBatchSaleRequest(
            transaction_date=_MANANA,
            items=[
                {
                    "product_id": "11111111-1111-1111-1111-111111111111",
                    "quantity": 1,
                    "unit_price": "10.00",
                }
            ],
        )


@pytest.mark.usefixtures("dia_ar_fijo")
def test_bulk_y_gasto_tambien_usan_el_dia_ar() -> None:
    """Los 7 validadores del módulo comparten la misma frontera."""
    with pytest.raises(ValueError, match="future"):
        BulkSaleRequest(
            period_type="daily", period_date=_MANANA.date(), total_amount_ars="100.00"
        )
    with pytest.raises(ValueError, match="future"):
        CreateExpenseRequest(amount="100.00", expense_date=_MANANA, category="other")


@pytest.mark.usefixtures("dia_ar_fijo")
def test_el_borde_es_el_dia_ar_completo() -> None:
    """Las 00:00 del día AR se aceptan; el mismo instante del día siguiente, no."""
    inicio = datetime.combine(_DIA_AR, datetime.min.time())
    assert (
        CreateSaleRequest(
            amount="1.00", transaction_date=inicio, payment_method="cash"
        ).transaction_date
        == inicio
    )
    with pytest.raises(ValueError, match="future"):
        CreateSaleRequest(
            amount="1.00", transaction_date=inicio + timedelta(days=1), payment_method="cash"
        )


# ── Los otros dos validadores del mismo camino ───────────────────────────────
# No están en `schemas/transaction.py`, así que el primer barrido los dejó afuera.
# `purchase` es uno de los `QueuedKind` de la cola offline y "Otros" resuelve filas
# a compras: los dos producen gasto COGS, movimiento de caja y ledger de inventario.


@pytest.fixture
def dia_ar_fijo_purchase():
    with unittest.mock.patch("app.schemas.purchase.today_ar", return_value=_DIA_AR):
        yield


@pytest.mark.usefixtures("dia_ar_fijo_purchase")
def test_una_compra_manual_fechada_manana_se_rechaza() -> None:
    with pytest.raises(ValueError, match="future"):
        ManualPurchaseRequest(
            transaction_date=_MANANA,
            supplier_id="22222222-2222-2222-2222-222222222222",
            lines=[_LINEA_DE_COMPRA],
        )


@pytest.mark.usefixtures("dia_ar_fijo_purchase")
def test_una_compra_manual_de_las_2350_ar_entra() -> None:
    compra = ManualPurchaseRequest(
        transaction_date=_HOY_2350,
        supplier_id="22222222-2222-2222-2222-222222222222",
        lines=[_LINEA_DE_COMPRA],
    )
    assert compra.transaction_date == _HOY_2350


def test_otros_resolver_a_compra_usa_el_dia_ar() -> None:
    from app.api.v1.others import ResolvePurchaseRequest

    campos = {
        "target_product_id": "11111111-1111-1111-1111-111111111111",
        "amount": "100.00",
        "quantity": 1,
    }
    with unittest.mock.patch("app.api.v1.others.today_ar", return_value=_DIA_AR):
        with pytest.raises(ValueError, match="future"):
            ResolvePurchaseRequest(**campos, transaction_date=_MANANA)
        ok = ResolvePurchaseRequest(**campos, transaction_date=_HOY_2350)
        assert ok.transaction_date == _HOY_2350
