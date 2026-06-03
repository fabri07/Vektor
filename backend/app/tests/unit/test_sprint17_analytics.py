"""Tests de las funciones analíticas determinísticas (Sprint 17)."""

from app.application.agents.shared import analytics

# ── parse_money (type-aware, Sprint 17 fix) ───────────────────────────────────


def test_parse_money_preserves_floats():
    # bug fix: un float numérico NO debe corromperse (1234.56 != 123456)
    assert analytics.parse_money(1234.56) == 1234.56
    assert analytics.parse_money(5000) == 5000.0


def test_parse_money_string_formats():
    assert analytics.parse_money("1234.56") == 1234.56  # float plano
    assert analytics.parse_money("1.234,56") == 1234.56  # AR: miles . decimal ,
    assert analytics.parse_money("1.234") == 1234.0  # AR miles (grupo de 3)
    assert analytics.parse_money("12.50") == 12.5  # decimal (2 dígitos)
    assert analytics.parse_money("$ 1.234.567") == 1234567.0
    assert analytics.parse_money("1234,5") == 1234.5


def test_parse_money_invalid():
    assert analytics.parse_money("") is None
    assert analytics.parse_money(None) is None
    assert analytics.parse_money("abc") is None
    assert analytics.parse_money(True) is None  # bool no es monto


def _prod(pid, name, price, cost, stock=0, margin=None):
    margin_abs = round(price - cost, 2) if cost is not None else None
    return {
        "product_id": pid,
        "name": name,
        "sku": None,
        "category": None,
        "sale_price": price,
        "unit_cost": cost,
        "stock_units": stock,
        "margin_pct": margin
        if margin is not None
        else (round((price - cost) / price * 100, 1) if cost is not None and price else None),
        "margin_abs": margin_abs,
    }


# ── Precios y márgenes ────────────────────────────────────────────────────────


def test_rank_by_margin_separates_no_cost():
    prods = [_prod("1", "A", 1000, 600), _prod("2", "B", 500, None)]
    r = analytics.rank_by_margin(prods)
    assert len(r["with_margin"]) == 1
    assert len(r["without_cost"]) == 1
    assert r["avg_margin_pct"] == 40.0


def test_suggest_sale_prices():
    prods = [_prod("1", "A", 1000, 600)]
    out = analytics.suggest_sale_prices(prods, target_margin_pct=50.0)
    assert out[0]["suggested_price"] == 1200.0  # 600 / (1-0.5)


def test_suggest_skips_products_without_cost():
    prods = [_prod("1", "A", 1000, None)]
    assert analytics.suggest_sale_prices(prods, 40.0) == []


def test_sensitive_products():
    prods = [_prod("1", "A", 1000, 620)]  # margen 38%
    out = analytics.sensitive_products(prods, warning_margin_pct=37.0, cost_bump_pct=5.0)
    # +5% costo → 651 → margen ~34.9% < 37 → sensible
    assert len(out) == 1
    assert out[0]["margin_after_bump_pct"] < 37.0


def test_diff_price_lists():
    old = {"coca": 1000.0, "pan": 500.0}
    new = {"coca": 1200.0, "pan": 500.0}
    diff = analytics.diff_price_lists(old, new)
    coca = next(d for d in diff if d["key"] == "coca")
    assert coca["delta_pct"] == 20.0


def test_simulate_price_increase():
    prods = [_prod("1", "A", 1000, 600)]
    sim = analytics.simulate_price_increase(prods, pct=10.0)
    assert sim["items"][0]["new_price"] == 1100.0
    assert sim["avg_margin_after_pct"] > sim["avg_margin_before_pct"]


# ── Stock ─────────────────────────────────────────────────────────────────────


def test_estimate_stock_days():
    prods = [{"product_id": "1", "name": "A", "stock_units": 10}]
    rows = analytics.estimate_stock_days(prods, {"1": 2.0})
    assert rows[0]["days_left"] == 5.0


def test_estimate_stock_days_no_velocity_is_none():
    prods = [{"product_id": "1", "name": "A", "stock_units": 10}]
    rows = analytics.estimate_stock_days(prods, {})
    assert rows[0]["days_left"] is None


def test_detect_overstock_includes_no_rotation():
    prods = [{"product_id": "1", "name": "A", "stock_units": 100}]
    over = analytics.detect_overstock(prods, {}, overstock_days=30)
    assert over[0]["reason"] == "sin_rotacion"


def test_replenishment_priority_no_stock_first():
    prods = [
        {"product_id": "1", "name": "A", "stock_units": 0},
        {"product_id": "2", "name": "B", "stock_units": 50},
    ]
    ranking = analytics.replenishment_priority(prods, {"2": 1.0}, lambda x: 0)
    assert ranking[0]["name"] == "A"  # sin stock primero


# ── Ventas ────────────────────────────────────────────────────────────────────


def test_join_sales_with_products():
    sales = [{"product_id": "1", "units": 10, "revenue": 10000.0, "n_sales": 5}]
    prods = [_prod("1", "A", 1000, 600)]
    joined = analytics.join_sales_with_products(sales, prods)
    assert joined[0]["estimated_profit"] == 4000.0  # 10000 * 40%


def test_classify_star_vs_problem():
    joined = [
        {"product_id": "1", "name": "Star", "revenue": 10000.0, "margin_pct": 45.0},
        {"product_id": "2", "name": "Problem", "revenue": 9000.0, "margin_pct": 10.0},
        {"product_id": "3", "name": "NoCost", "revenue": 100.0, "margin_pct": None},
    ]
    c = analytics.classify_star_vs_problem(joined, warning_margin_pct=25.0)
    assert any(s["name"] == "Star" for s in c["stars"])
    assert any(p["name"] == "Problem" for p in c["problems"])
    assert len(c["no_cost"]) == 1


# ── Gastos ────────────────────────────────────────────────────────────────────


def test_detect_expense_anomalies():
    stats = {"RENT": {"count": 4, "mean": 100.0, "stddev": 10.0, "amounts": [100, 100, 100, 200]}}
    out = analytics.detect_expense_anomalies(stats, sigma=2.0)
    assert out[0]["category"] == "RENT"
    assert 200 in out[0]["outliers"]


def test_anomalies_requires_min_samples():
    stats = {"X": {"count": 2, "mean": 100.0, "stddev": 50.0, "amounts": [50, 150]}}
    assert analytics.detect_expense_anomalies(stats) == []


def test_breakeven_point():
    assert analytics.breakeven_point(100000, 40.0) == 250000.0
    assert analytics.breakeven_point(100000, None) is None
    assert analytics.breakeven_point(100000, 0) is None
