"""Test de reconciliación — LA compuerta antes de que el frontend deje de calcular.

Este test NO valida contra "el número viejo" (había cuatro y se contradecían).
Valida contra las DECISIONES CANÓNICAS ratificadas. Si pasa verde, el número de
FactsService ES el número correcto por definición, y recién ahí el frontend puede
dejar de agregar en el navegador.

Decisiones cubiertas:
    1. Ventas: borde superior DURO (sin futuras), sin DEMO por default, sin blend.
    2. Margen bruto ≠ neto; fallback COGS por expense_type (nunca por nombre).
    3. Fiado = deuda, NO caja. Tarjeta de crédito = delayed, NO caja (ratificado).
    4. Cobros de fiado ("inflow"): NO son ventas, SÍ son caja.
    5. Stock centralizado + sobrestock.
    6. EMPTY correcto: value=None, nunca $0 falso.
    7. Invariante global: dashboard y chat leen el MISMO hecho.

La reconciliación contra la DB real (provider + 90 días sintéticos) vive en
test_facts_provider.py.
"""

from datetime import date, timedelta

import pandas as pd
import pytest

from app.application.services.facts_service import (
    FactsService,
    Period,
    Provenance,
    SeverityThresholds,
)
from app.domain.verticals import Vertical
from app.heuristics.verticals.loader import load_margin_benchmark

TODAY = date(2026, 6, 30)  # ancla determinista para los tests

# Umbrales EXPLÍCITOS del vertical: `SeverityThresholds` ya no trae los de kiosco
# de default, así que el servicio de prueba declara cuál es su rubro. Salen del
# MISMO benchmark JSON que usa el health engine (igual que
# `facts_provider.thresholds_for_vertical`), no de números copiados a mano.
_KIOSCO_MARGIN = load_margin_benchmark(Vertical.KIOSCO_ALMACEN)
KIOSCO_THRESHOLDS = SeverityThresholds(
    margen_neto_floor=float(_KIOSCO_MARGIN.warning_below) * 100.0,
    margen_neto_critical=float(_KIOSCO_MARGIN.critical_below) * 100.0,
)

_SALES_COLS = ["sale_date", "amount_ars", "cost_ars", "payment_method", "provenance"]
_EXPENSES_COLS = ["expense_date", "amount_ars", "category", "expense_type", "provenance"]
_PRODUCTS_COLS = [
    "product_id", "stock_units", "unit_cost_ars", "last_sold_date",
    "first_seen_date", "provenance",
]


class FakeProvider:
    """Provider en memoria. El provider real (facts_provider.py) se testea contra SQLite."""

    def __init__(self, sales: pd.DataFrame, expenses: pd.DataFrame, products: pd.DataFrame):
        self._s, self._e, self._p = sales, expenses, products

    def sales(self, tenant_id, start, end):
        d = pd.to_datetime(self._s["sale_date"]).dt.date
        return self._s[(d >= start) & (d <= end)].copy()

    def expenses(self, tenant_id, start, end):
        d = pd.to_datetime(self._e["expense_date"]).dt.date
        return self._e[(d >= start) & (d <= end)].copy()

    def products(self, tenant_id):
        return self._p.copy()


def _service(sales_rows: list[dict], expenses_rows: list[dict] | None = None,
             products_rows: list[dict] | None = None) -> FactsService:
    sales = pd.DataFrame(sales_rows, columns=_SALES_COLS)
    expenses = pd.DataFrame(expenses_rows or [], columns=_EXPENSES_COLS)
    products = pd.DataFrame(products_rows or [], columns=_PRODUCTS_COLS)
    return FactsService(FakeProvider(sales, expenses, products), KIOSCO_THRESHOLDS)


@pytest.fixture
def svc():
    sales = pd.DataFrame([
        # dentro de [hoy-29, hoy]
        {"sale_date": TODAY, "amount_ars": 10000, "cost_ars": 6000,
         "payment_method": "cash", "provenance": "REAL"},
        {"sale_date": TODAY - timedelta(days=5), "amount_ars": 5000, "cost_ars": 3000,
         "payment_method": "account", "provenance": "REAL"},  # fiado canónico = account
        {"sale_date": TODAY - timedelta(days=10), "amount_ars": 8000, "cost_ars": 5000,
         "payment_method": "qr", "provenance": "REAL"},
        # FUERA de ventana por borde superior: fecha futura (bug clásico que este test caza)
        {"sale_date": TODAY + timedelta(days=2), "amount_ars": 99999, "cost_ars": 1,
         "payment_method": "cash", "provenance": "REAL"},
        # período anterior [hoy-59, hoy-30]
        {"sale_date": TODAY - timedelta(days=40), "amount_ars": 20000, "cost_ars": 12000,
         "payment_method": "cash", "provenance": "REAL"},
        # DEMO: no debe contar salvo include_demo=True
        {"sale_date": TODAY, "amount_ars": 777, "cost_ars": 0,
         "payment_method": "cash", "provenance": "DEMO"},
    ])
    expenses = pd.DataFrame([
        {"expense_date": TODAY, "amount_ars": 4000, "category": "INVENTORY",
         "expense_type": "COGS", "provenance": "REAL"},
        {"expense_date": TODAY - timedelta(days=3), "amount_ars": 1000, "category": "RENT",
         "expense_type": "OPEX", "provenance": "REAL"},
    ])
    products = pd.DataFrame([
        {"product_id": "coca500", "stock_units": 100, "unit_cost_ars": 800,
         "last_sold_date": TODAY - timedelta(days=1),
         "first_seen_date": TODAY - timedelta(days=300), "provenance": "REAL"},
        {"product_id": "lavandina", "stock_units": 50, "unit_cost_ars": 500,
         "last_sold_date": TODAY - timedelta(days=200),
         "first_seen_date": TODAY - timedelta(days=300), "provenance": "REAL"},  # sobrestock
    ])
    return FactsService(FakeProvider(sales, expenses, products), KIOSCO_THRESHOLDS)


P30 = Period.last_n_days(30, today=TODAY)


# ─────────────────────────────────────────────────────────────────────────────
# DECISIÓN 1 — Ventas: ventana con borde superior DURO, sin futuras, sin blend, sin DEMO
# ─────────────────────────────────────────────────────────────────────────────
def test_ventas_excluye_fecha_futura(svc):
    f = svc.ventas_periodo("t", P30)
    # 10000 + 5000 + 8000 = 23000. La venta futura (99999) NO entra.
    assert f.value == 23000, "El borde superior duro debe excluir ventas de fecha futura"


def test_ventas_excluye_demo_por_default(svc):
    f = svc.ventas_periodo("t", P30)
    assert f.value == 23000  # los 777 DEMO no cuentan
    f_demo = svc.ventas_periodo("t", P30, include_demo=True)
    assert f_demo.value == 23777


def test_ventas_variacion_vs_periodo_anterior(svc):
    f = svc.ventas_periodo("t", P30)
    assert f.comparison_value == 20000       # período anterior
    assert f.variation_pct == 15.0           # (23000-20000)/20000*100


def test_ventas_pocas_baja_confidence_sin_blend(svc):
    f = svc.ventas_periodo("t", P30)
    assert f.provenance == Provenance.REAL    # NUNCA estimación silenciosa
    assert f.sample_size == 3
    assert f.confidence < 1.0                 # 3 < 50 → confidence reducido
    assert f.value == 23000                   # pero el número es el REAL, no un blend


# ─────────────────────────────────────────────────────────────────────────────
# DECISIÓN 2 — Margen bruto ≠ neto, definiciones separadas
# ─────────────────────────────────────────────────────────────────────────────
def test_margen_bruto_usa_cogs(svc):
    f = svc.margen_bruto("t", P30)
    # ventas 23000, COGS por line-items = 6000+3000+5000 = 14000
    # (23000-14000)/23000*100 = 39.13%
    assert f.value == 39.1


def test_margen_neto_resta_todos_los_gastos(svc):
    f = svc.margen_neto("t", P30)
    # ventas 23000, gastos totales = 4000+1000 = 5000
    # (23000-5000)/23000*100 = 78.26%
    assert f.value == 78.3


def test_bruto_y_neto_son_distintos(svc):
    assert svc.margen_bruto("t", P30).value != svc.margen_neto("t", P30).value


def test_heuristica_severity_sobre_neto(svc):
    # el health score corre sobre NETO: verificamos que margen_neto lleva severity
    f = svc.margen_neto("t", P30)
    assert f.severity in ("info", "warning", "critical")


def test_cogs_fallback_usa_expense_type_no_categoria():
    # Sin cost_ars por venta → fallback a gastos expense_type=COGS (frontera canónica).
    # La categoría se llama INVENTORY pero da igual: lo que manda es expense_type.
    svc = _service(
        sales_rows=[{"sale_date": TODAY, "amount_ars": 10000, "cost_ars": None,
                     "payment_method": "cash", "provenance": "REAL"}],
        expenses_rows=[
            {"expense_date": TODAY, "amount_ars": 4000, "category": "INVENTORY",
             "expense_type": "COGS", "provenance": "REAL"},
            {"expense_date": TODAY, "amount_ars": 1000, "category": "RENT",
             "expense_type": "OPEX", "provenance": "REAL"},
        ],
    )
    f = svc.margen_bruto("t", P30)
    assert f.value == 60.0                    # (10000-4000)/10000 — solo el gasto COGS
    assert f.confidence == 0.7                # proxy por caja, no line-items
    assert "cogs_expense" in f.source


def test_cogs_solo_opex_cae_a_sin_cogs():
    # Hay gastos pero NINGUNO es COGS: el "margen 100%" sale con confidence mínima,
    # nunca etiquetado como proxy válido (cogs_expense).
    svc = _service(
        sales_rows=[{"sale_date": TODAY, "amount_ars": 10000, "cost_ars": None,
                     "payment_method": "cash", "provenance": "REAL"}],
        expenses_rows=[{"expense_date": TODAY, "amount_ars": 1000, "category": "RENT",
                        "expense_type": "OPEX", "provenance": "REAL"}],
    )
    f = svc.margen_bruto("t", P30)
    assert f.value == 100.0
    assert f.confidence == 0.4
    assert "sin_cogs" in f.source


def test_margen_neto_sin_gastos_degrada_confidence():
    # Cero gastos cargados ≠ cero gastos reales: el 100% sale con confidence 0.4.
    svc = _service(
        sales_rows=[{"sale_date": TODAY, "amount_ars": 10000, "cost_ars": None,
                     "payment_method": "cash", "provenance": "REAL"}],
    )
    f = svc.margen_neto("t", P30)
    assert f.value == 100.0
    assert f.confidence == 0.4
    assert "sin_gastos" in f.source


def test_cogs_parcial_line_items_declara_cobertura():
    # Con cost_ars en ALGUNAS ventas se usan line-items, pero la confidence es la
    # COBERTURA por monto (regla "agregados no engañosos"), no un 1.0 pleno.
    svc = _service(
        sales_rows=[
            {"sale_date": TODAY, "amount_ars": 10000, "cost_ars": 6000,
             "payment_method": "cash", "provenance": "REAL"},
            {"sale_date": TODAY, "amount_ars": 5000, "cost_ars": None,
             "payment_method": "cash", "provenance": "REAL"},
        ],
    )
    f = svc.margen_bruto("t", P30)
    # ventas 15000, COGS = 6000 (solo la venta con costo) → (15000-6000)/15000 = 60.0
    assert f.value == 60.0
    assert f.confidence == 0.67          # cobertura = 10000/15000
    assert "line_items" in f.source
    assert "67%" in f.source             # la cobertura queda auditada en source


def test_cogs_cobertura_infima_no_da_confianza_plena():
    # 1 venta costeada sobre 10: el margen sale inflado pero la confidence lo grita.
    rows = [{"sale_date": TODAY, "amount_ars": 1000, "cost_ars": None,
             "payment_method": "cash", "provenance": "REAL"} for _ in range(9)]
    rows.append({"sale_date": TODAY, "amount_ars": 1000, "cost_ars": 600,
                 "payment_method": "cash", "provenance": "REAL"})
    f = _service(sales_rows=rows).margen_bruto("t", P30)
    assert f.confidence == 0.1           # cobertura 1000/10000 — nunca 1.0


# ─────────────────────────────────────────────────────────────────────────────
# DECISIÓN 3 — Fiado = deuda NO caja; tarjeta de crédito = delayed NO caja
# ─────────────────────────────────────────────────────────────────────────────
def test_caja_liquida_excluye_fiado(svc):
    f = svc.caja_liquida("t", P30)
    # 23000 total − 5000 fiado = 18000 líquido
    assert f.value == 18000


def test_fiado_es_metrica_de_deuda_separada(svc):
    f = svc.fiado_pendiente("t", P30)
    assert f.value == 5000
    assert f.domain == "caja"
    assert f.metric == "fiado_pendiente"


def test_ingresos_totales_vs_caja(svc):
    # invariante (sin inflows ni delayed): caja_liquida + fiado == ingresos totales
    caja = svc.caja_liquida("t", P30).value
    fiado = svc.fiado_pendiente("t", P30).value
    assert caja + fiado == 23000


def test_credit_card_es_delayed_no_caja():
    # RATIFICADO: la tarjeta se acredita ~30d → es venta reconocida pero NO caja.
    svc = _service(
        sales_rows=[
            {"sale_date": TODAY, "amount_ars": 10000, "cost_ars": None,
             "payment_method": "cash", "provenance": "REAL"},
            {"sale_date": TODAY, "amount_ars": 6000, "cost_ars": None,
             "payment_method": "credit_card", "provenance": "REAL"},
        ],
    )
    assert svc.ventas_periodo("t", P30).value == 16000   # la venta cuenta
    assert svc.caja_liquida("t", P30).value == 10000     # la plata todavía no


# ─────────────────────────────────────────────────────────────────────────────
# DECISIÓN 4 — Cobros de fiado ("inflow"): NO son ventas, SÍ son caja
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def svc_con_inflows():
    return _service(
        sales_rows=[
            {"sale_date": TODAY, "amount_ars": 10000, "cost_ars": None,
             "payment_method": "cash", "provenance": "REAL"},
            {"sale_date": TODAY - timedelta(days=2), "amount_ars": 6000, "cost_ars": None,
             "payment_method": "account", "provenance": "REAL"},       # fiado
            {"sale_date": TODAY - timedelta(days=3), "amount_ars": 4000, "cost_ars": None,
             "payment_method": "credit_card", "provenance": "REAL"},   # delayed
            # cobro de un fiado viejo: entra plata, NO es una venta nueva
            {"sale_date": TODAY - timedelta(days=1), "amount_ars": 2500, "cost_ars": None,
             "payment_method": "inflow", "provenance": "REAL"},
        ],
    )


def test_inflow_excluido_de_ventas_y_ticket(svc_con_inflows):
    f = svc_con_inflows.ventas_periodo("t", P30)
    assert f.value == 20000        # 10000+6000+4000; el cobro de 2500 NO es venta
    assert f.sample_size == 3      # el inflow tampoco cuenta como transacción de venta
    t = svc_con_inflows.ticket_promedio("t", P30)
    assert t.value == round(20000 / 3, 2)  # el ticket no se diluye con cobros


def test_inflow_suma_a_caja_liquida(svc_con_inflows):
    f = svc_con_inflows.caja_liquida("t", P30)
    # 20000 ventas − 6000 fiado − 4000 tarjeta + 2500 cobro = 12500
    assert f.value == 12500


def test_invariante_caja_con_inflows(svc_con_inflows):
    # caja_liquida − inflows + fiado + delayed == ventas_totales
    caja = svc_con_inflows.caja_liquida("t", P30).value
    fiado = svc_con_inflows.fiado_pendiente("t", P30).value
    ventas = svc_con_inflows.ventas_periodo("t", P30).value
    inflows, delayed = 2500, 4000
    assert caja - inflows + fiado + delayed == ventas


# ─────────────────────────────────────────────────────────────────────────────
# DECISIÓN 5 — stock centralizado + sobrestock
# ─────────────────────────────────────────────────────────────────────────────
def test_valor_stock_formula_unica(svc):
    f = svc.valor_stock("t")
    # 100*800 + 50*500 = 80000 + 25000 = 105000
    assert f.value == 105000


def test_sobrestock_detecta_producto_congelado(svc):
    facts = svc.sobrestock("t", today=TODAY)
    ids = {f.fact_id for f in facts}
    assert "sobrestock_lavandina" in ids   # 200 días sin vender
    assert "sobrestock_coca500" not in ids  # vendido ayer


def test_sobrestock_no_flaggea_producto_recien_cargado():
    # Nunca vendido pero dado de alta AYER: es stock nuevo, no capital inmovilizado.
    # Nunca vendido y dado de alta hace 180 días: eso SÍ es sobrestock.
    svc = _service(
        sales_rows=[],
        products_rows=[
            {"product_id": "nuevo", "stock_units": 30, "unit_cost_ars": 100,
             "last_sold_date": None, "first_seen_date": TODAY - timedelta(days=1),
             "provenance": "REAL"},
            {"product_id": "viejo", "stock_units": 30, "unit_cost_ars": 100,
             "last_sold_date": None, "first_seen_date": TODAY - timedelta(days=180),
             "provenance": "REAL"},
            # sin NINGUNA fecha: no se puede afirmar nada → no se flaggea (no-invention)
            {"product_id": "sin_fechas", "stock_units": 5, "unit_cost_ars": 100,
             "last_sold_date": None, "first_seen_date": None, "provenance": "REAL"},
        ],
    )
    ids = {f.fact_id for f in svc.sobrestock("t", today=TODAY)}
    assert ids == {"sobrestock_viejo"}


def test_valor_stock_sin_costos_es_empty_no_cero():
    # Todos los productos sin unit_cost_ars (post-import típico): EMPTY con None,
    # nunca "$0 de stock" con provenance REAL.
    svc = _service(
        sales_rows=[],
        products_rows=[
            {"product_id": "a", "stock_units": 10, "unit_cost_ars": None,
             "last_sold_date": None, "first_seen_date": TODAY, "provenance": "REAL"},
        ],
    )
    f = svc.valor_stock("t")
    assert f.provenance == Provenance.EMPTY
    assert f.value is None


def test_valor_stock_cobertura_parcial_baja_confidence():
    svc = _service(
        sales_rows=[],
        products_rows=[
            {"product_id": "a", "stock_units": 10, "unit_cost_ars": 100,
             "last_sold_date": None, "first_seen_date": TODAY, "provenance": "REAL"},
            {"product_id": "b", "stock_units": 10, "unit_cost_ars": None,
             "last_sold_date": None, "first_seen_date": TODAY, "provenance": "REAL"},
        ],
    )
    f = svc.valor_stock("t")
    assert f.value == 1000.0        # solo el costeado
    assert f.confidence == 0.5      # 1 de 2 con costo
    assert "1/2" in f.source


# ─────────────────────────────────────────────────────────────────────────────
# INVARIANTE GLOBAL — dashboard y chat leen el MISMO objeto
# ─────────────────────────────────────────────────────────────────────────────
def test_dashboard_y_chat_leen_el_mismo_hecho(svc):
    snap = svc.dashboard_snapshot("t", P30)
    advice_facts = svc.collect_for_advice("t", "flujo_caja", P30)
    # el fact de caja que ve el chat es idéntico al que pinta el dashboard
    caja_dash = snap["caja_liquida"].value
    caja_chat = next(f for f in advice_facts if f.metric == "caja_liquida").value
    assert caja_dash == caja_chat


# ─────────────────────────────────────────────────────────────────────────────
# EMPTY correcto — nunca inventar cuando no hay datos
# ─────────────────────────────────────────────────────────────────────────────
def test_sin_datos_devuelve_empty_no_cero_falso():
    empty = _service(sales_rows=[])
    f = empty.ventas_periodo("t", P30)
    assert f.provenance == Provenance.EMPTY
    assert f.value is None          # None, NO 0 — el chat debe decir "sin datos", no "$0"
    assert f.confidence == 0.0


def test_caja_y_fiado_sin_datos_tambien_son_none():
    # El invariante "nunca $0 falso" aplica a TODAS las métricas, no solo ventas.
    empty = _service(sales_rows=[])
    caja = empty.caja_liquida("t", P30)
    fiado = empty.fiado_pendiente("t", P30)
    assert caja.provenance == Provenance.EMPTY and caja.value is None
    assert fiado.provenance == Provenance.EMPTY and fiado.value is None
    assert caja.confidence == 0.0 and fiado.confidence == 0.0
