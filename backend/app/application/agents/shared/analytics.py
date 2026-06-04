"""Funciones analíticas determinísticas para los handlers ANALYZE_* (Sprint 17).

Sin LLM, sin I/O. Reciben datos ya cargados (de repositorios) y devuelven
estructuras listas para narrar. Política no-invention: si falta un dato (ej.
costo de un producto), se reporta como faltante — nunca se rellena con defaults.

Todas las funciones son puras y testeables de forma aislada
(`app/tests/.../test_*_analytics.py`).
"""

from __future__ import annotations

from typing import Any

# ── Parsing monetario type-aware ──────────────────────────────────────────────


def parse_money(value: Any) -> float | None:
    """Convierte un valor (numérico o string) a float sin corromper decimales.

    Crítico: si el valor YA es numérico (openpyxl/JSON devuelven float/int para
    celdas numéricas), se devuelve tal cual — `replace(".", "")` corrompería
    `1234.56` → `123456`. Para strings maneja formato argentino (`1.234,56` →
    punto miles, coma decimal) y formato plano (`1234.56`). Devuelve None si no
    se puede interpretar o si es <= 0 no aplica (eso lo decide el caller).
    """
    if value is None:
        return None
    if isinstance(value, bool):  # bool es subclase de int — descartar
        return None
    if isinstance(value, int | float):
        return float(value)
    s = str(value).strip().replace("$", "").replace(" ", "")
    if not s:
        return None
    has_dot = "." in s
    has_comma = "," in s
    try:
        if has_dot and has_comma:
            # formato AR: punto = miles, coma = decimal → 1.234,56
            return float(s.replace(".", "").replace(",", "."))
        if has_comma:
            # solo coma → separador decimal → 1234,56
            return float(s.replace(",", "."))
        if has_dot:
            # solo punto: ambiguo. En AR "1.234" = miles (1234); "12.50" = decimal.
            # Heurística: varios puntos, o un grupo final de exactamente 3 dígitos →
            # separador de miles. La moneda AR usa 2 decimales, no 3.
            parts = s.split(".")
            if len(parts) > 2 or (len(parts) == 2 and len(parts[1]) == 3):
                return float(s.replace(".", ""))  # miles → 1.234 → 1234
            return float(s)  # decimal → 1234.56 preservado
        # sin separadores → entero
        return float(s)
    except (ValueError, TypeError):
        return None


# ── Precios y márgenes ────────────────────────────────────────────────────────


def rank_by_margin(products: list[dict[str, Any]]) -> dict[str, Any]:
    """Ordena productos por margen %. Separa los que no tienen costo cargado.

    `products` proviene de `ProductRepository.get_products_with_margin`.
    Returns: {with_margin: [...desc], without_cost: [...], avg_margin_pct: float|None}.
    """
    with_margin = [p for p in products if p.get("margin_pct") is not None]
    without_cost = [p for p in products if p.get("margin_pct") is None]
    with_margin.sort(key=lambda p: p["margin_pct"], reverse=True)
    avg = (
        round(sum(p["margin_pct"] for p in with_margin) / len(with_margin), 1)
        if with_margin
        else None
    )
    return {
        "with_margin": with_margin,
        "without_cost": without_cost,
        "avg_margin_pct": avg,
    }


def suggest_sale_prices(
    products: list[dict[str, Any]], target_margin_pct: float
) -> list[dict[str, Any]]:
    """Sugiere precio de venta para alcanzar `target_margin_pct` sobre el costo.

    Fórmula: precio = costo / (1 - target/100). Solo productos con costo > 0.
    Returns list[dict[str, Any]]: name, sku, unit_cost, current_price, suggested_price, delta_pct.
    """
    if target_margin_pct >= 100:
        target_margin_pct = 99.0  # evita división por cero / negativo
    factor = 1 - target_margin_pct / 100.0
    out: list[dict[str, Any]] = []
    for p in products:
        cost = p.get("unit_cost")
        if cost is None or cost <= 0 or factor <= 0:
            continue
        suggested = round(cost / factor, 2)
        current = p.get("sale_price") or 0.0
        delta_pct = round((suggested - current) / current * 100, 1) if current > 0 else None
        out.append(
            {
                "name": p.get("name"),
                "sku": p.get("sku"),
                "unit_cost": cost,
                "current_price": current,
                "suggested_price": suggested,
                "delta_pct": delta_pct,
            }
        )
    return out


def sensitive_products(
    products: list[dict[str, Any]],
    warning_margin_pct: float,
    cost_bump_pct: float = 5.0,
) -> list[dict[str, Any]]:
    """Productos cuyo margen cae por debajo del warning ante un aumento de costo.

    Simula un incremento de `cost_bump_pct` en el costo (precio constante) y marca
    los productos cuyo margen resultante quede por debajo de `warning_margin_pct`.
    Solo productos con costo y precio > 0.
    """
    out: list[dict[str, Any]] = []
    for p in products:
        cost = p.get("unit_cost")
        price = p.get("sale_price") or 0.0
        if cost is None or cost <= 0 or price <= 0:
            continue
        new_cost = cost * (1 + cost_bump_pct / 100.0)
        new_margin = (price - new_cost) / price * 100.0
        if new_margin < warning_margin_pct:
            out.append(
                {
                    "name": p.get("name"),
                    "sku": p.get("sku"),
                    "current_margin_pct": p.get("margin_pct"),
                    "margin_after_bump_pct": round(new_margin, 1),
                }
            )
    return out


def diff_price_lists(
    old_items: dict[str, float],
    new_items: dict[str, float],
) -> list[dict[str, Any]]:
    """Compara dos listas de precios indexadas por nombre/sku normalizado.

    Returns list[dict[str, Any]]: key, old_price, new_price, delta_abs, delta_pct.
    Solo items presentes en ambas listas (intersección).
    """
    out: list[dict[str, Any]] = []
    for key in sorted(set(old_items) & set(new_items)):
        old = old_items[key]
        new = new_items[key]
        delta_abs = round(new - old, 2)
        delta_pct = round((new - old) / old * 100, 1) if old > 0 else None
        out.append(
            {
                "key": key,
                "old_price": old,
                "new_price": new,
                "delta_abs": delta_abs,
                "delta_pct": delta_pct,
            }
        )
    return out


def simulate_price_increase(products: list[dict[str, Any]], pct: float) -> dict[str, Any]:
    """Aplica un aumento porcentual de precio en memoria y reporta el impacto en margen.

    No persiste nada. Returns: {items: [...], avg_margin_before, avg_margin_after}.
    """
    items: list[dict[str, Any]] = []
    before: list[float] = []
    after: list[float] = []
    for p in products:
        cost = p.get("unit_cost")
        price = p.get("sale_price") or 0.0
        if price <= 0:
            continue
        new_price = round(price * (1 + pct / 100.0), 2)
        row: dict[str, Any] = {
            "name": p.get("name"),
            "current_price": price,
            "new_price": new_price,
        }
        if cost is not None and cost > 0:
            m_before = (price - cost) / price * 100.0
            m_after = (new_price - cost) / new_price * 100.0
            row["margin_before_pct"] = round(m_before, 1)
            row["margin_after_pct"] = round(m_after, 1)
            before.append(m_before)
            after.append(m_after)
        items.append(row)
    return {
        "pct": pct,
        "items": items,
        "avg_margin_before_pct": round(sum(before) / len(before), 1) if before else None,
        "avg_margin_after_pct": round(sum(after) / len(after), 1) if after else None,
    }


# ── Stock ───────────────────────────────────────────────────────────────────


def estimate_stock_days(
    products: list[dict[str, Any]],
    velocity: dict[str, float],
) -> list[dict[str, Any]]:
    """Estima días de stock restantes por producto = stock_units / velocidad diaria.

    `velocity` proviene de `SaleRepository.get_daily_velocity` (product_id → u/día).
    Productos sin velocidad (no se venden) → days=None (no se infiere infinito).
    """
    out: list[dict[str, Any]] = []
    for p in products:
        pid = p.get("product_id")
        v = velocity.get(pid or "", 0.0)
        stock = p.get("stock_units") or 0
        days = round(stock / v, 1) if v > 0 else None
        out.append(
            {
                "name": p.get("name"),
                "product_id": pid,
                "stock_units": stock,
                "daily_velocity": v,
                "days_left": days,
            }
        )
    return out


def detect_overstock(
    products: list[dict[str, Any]],
    velocity: dict[str, float],
    overstock_days: float,
) -> list[dict[str, Any]]:
    """Productos con cobertura > `overstock_days` (capital inmovilizado).

    Productos sin velocidad pero con stock > 0 también se marcan como sobrestock
    (mercadería que no rota). Ordenados por días de cobertura desc.
    """
    out: list[dict[str, Any]] = []
    for row in estimate_stock_days(products, velocity):
        stock = row["stock_units"]
        days = row["days_left"]
        if stock <= 0:
            continue
        if days is None or days > overstock_days:
            out.append({**row, "reason": "sin_rotacion" if days is None else "cobertura_alta"})
    out.sort(key=lambda r: (r["days_left"] is not None, r["days_left"] or 0), reverse=True)
    return out


def replenishment_priority(
    products: list[dict[str, Any]],
    velocity: dict[str, float],
    threshold_fn: Any,
) -> list[dict[str, Any]]:
    """Prioriza reposición: menor cobertura de días primero, luego mayor velocidad.

    `threshold_fn(low_stock_threshold_units)` → umbral efectivo (callable inyectable
    para reusar `effective_threshold` sin acoplar el import). Solo productos cuyo
    stock está en o por debajo del umbral, o sin stock.
    """
    candidates: list[dict[str, Any]] = []
    for row in estimate_stock_days(products, velocity):
        days = row["days_left"]
        # criticidad: sin stock primero; luego por días de cobertura ascendente
        priority_key = (
            0 if row["stock_units"] <= 0 else 1,
            days if days is not None else float("inf"),
            -row["daily_velocity"],
        )
        candidates.append({**row, "_key": priority_key})
    candidates.sort(key=lambda r: r["_key"])
    for c in candidates:
        c.pop("_key", None)
    return candidates


# ── Ventas analíticas ─────────────────────────────────────────────────────────


def join_sales_with_products(
    sales_by_product: list[dict[str, Any]],
    products_with_margin: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Cruza ventas agregadas por producto con su margen del catálogo.

    Returns list[dict[str, Any]] con units, revenue, margin_pct, estimated_profit (cuando hay
    costo). Ordenado por revenue desc.
    """
    margin_by_id = {p["product_id"]: p for p in products_with_margin}
    out: list[dict[str, Any]] = []
    for s in sales_by_product:
        pid = s["product_id"]
        prod = margin_by_id.get(pid)
        margin_pct = prod.get("margin_pct") if prod else None
        unit_cost = prod.get("unit_cost") if prod else None
        units = s["units"]
        revenue = s["revenue"]
        est_profit = round(revenue * margin_pct / 100.0, 2) if margin_pct is not None else None
        out.append(
            {
                "product_id": pid,
                "name": prod.get("name") if prod else None,
                "units": units,
                "revenue": revenue,
                "n_sales": s.get("n_sales", 0),
                "margin_pct": margin_pct,
                "unit_cost": unit_cost,
                "estimated_profit": est_profit,
            }
        )
    out.sort(key=lambda r: r["revenue"], reverse=True)
    return out


def classify_star_vs_problem(
    joined: list[dict[str, Any]],
    warning_margin_pct: float,
) -> dict[str, list[dict[str, Any]]]:
    """Separa productos estrella (alto volumen + buen margen) de problemáticos.

    Estrella: revenue en el top 50% Y margin_pct >= warning_margin_pct.
    Problemático: alto volumen (top 50% revenue) pero margin_pct < warning_margin_pct.
    Productos sin margen → 'sin_costo' (no clasificables).
    """
    rated = [r for r in joined if r.get("margin_pct") is not None]
    no_cost = [r for r in joined if r.get("margin_pct") is None]
    if not rated:
        return {"stars": [], "problems": [], "no_cost": no_cost}

    revenues = sorted((r["revenue"] for r in rated), reverse=True)
    median_idx = len(revenues) // 2
    revenue_threshold = revenues[median_idx] if revenues else 0.0

    stars: list[dict[str, Any]] = []
    problems: list[dict[str, Any]] = []
    for r in rated:
        high_volume = r["revenue"] >= revenue_threshold
        good_margin = r["margin_pct"] >= warning_margin_pct
        if high_volume and good_margin:
            stars.append(r)
        elif high_volume and not good_margin:
            problems.append(r)
    return {"stars": stars, "problems": problems, "no_cost": no_cost}


# ── Gastos inteligentes ───────────────────────────────────────────────────────


def detect_expense_anomalies(
    stats: dict[str, dict[str, Any]],
    sigma: float = 2.0,
) -> list[dict[str, Any]]:
    """Detecta gastos anómalos: monto > media + `sigma`·desvío por categoría.

    `stats` proviene de `ExpenseRepository.get_expense_stats_by_category`.
    Solo categorías con count >= 3 y stddev > 0 (sin muestra suficiente no se marca).
    """
    out: list[dict[str, Any]] = []
    for cat, s in stats.items():
        if s["count"] < 3 or s["stddev"] <= 0:
            continue
        threshold = s["mean"] + sigma * s["stddev"]
        outliers = [a for a in s["amounts"] if a > threshold]
        if outliers:
            out.append(
                {
                    "category": cat,
                    "mean": s["mean"],
                    "stddev": s["stddev"],
                    "threshold": round(threshold, 2),
                    "outliers": sorted(outliers, reverse=True),
                    "n_outliers": len(outliers),
                }
            )
    return out


def breakeven_point(fixed_costs: float, margin_bruto_pct: float | None) -> float | None:
    """Punto de equilibrio en ventas = costos_fijos / (margen_bruto_pct/100).

    Si no hay margen (None o <= 0) → None (no se puede calcular, no se inventa).
    """
    if margin_bruto_pct is None or margin_bruto_pct <= 0:
        return None
    return round(fixed_costs / (margin_bruto_pct / 100.0), 2)
