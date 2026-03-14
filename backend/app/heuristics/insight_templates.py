"""
Insight templates for each primary_risk_code.

Each template has a title, description, and action_text — all with
Python format-string placeholders filled from real tenant data.

Tone: direct, short, no emojis, no exaggeration.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class InsightTemplate:
    title_tpl: str
    description_tpl: str
    action_tpl: str


# ── Vertical display names ─────────────────────────────────────────────────────

_VERTICAL_NAMES: dict[str, str] = {
    "kiosco": "kiosco",
    "decoracion_hogar": "decoración hogar",
    "limpieza": "limpieza",
}

# ── Margin benchmarks (pct strings) per vertical ──────────────────────────────

_MARGIN_RANGES: dict[str, tuple[int, int]] = {
    "kiosco":           (18, 28),
    "decoracion_hogar": (30, 45),
    "limpieza":         (20, 35),
}

# ── Templates ─────────────────────────────────────────────────────────────────

TEMPLATES: dict[str, InsightTemplate] = {
    "CASH_LOW": InsightTemplate(
        title_tpl="Tu caja cubre menos de {dias_cobertura} días",
        description_tpl=(
            "Con {caja_ars} disponible y {gastos_fijos} de gastos fijos, "
            "tu cobertura operativa es limitada."
        ),
        action_tpl="Revisá gastos no esenciales o considerá un adelanto de ventas.",
    ),
    "MARGIN_LOW": InsightTemplate(
        title_tpl="Tu margen está por debajo del rango saludable para {vertical}",
        description_tpl=(
            "Estimamos un margen de {margin_pct}%. "
            "El rango saludable para tu rubro es {min}% – {max}%."
        ),
        action_tpl=(
            "Revisá el precio de tus productos de mayor volumen. "
            "Un ajuste del 5% puede recuperar {impacto_ars} de margen mensual."
        ),
    ),
    "STOCK_CRITICAL": InsightTemplate(
        title_tpl="{n_productos} productos están cerca de agotarse",
        description_tpl=(
            "Tenés productos con stock bajo que podrían generar "
            "quiebres de stock en los próximos días."
        ),
        action_tpl="Revisá y reponé: {lista_productos_criticos}.",
    ),
    "SUPPLIER_DEPENDENCY": InsightTemplate(
        title_tpl="Dependés de un solo proveedor",
        description_tpl=(
            "Un único proveedor concentra tu reposición. "
            "Cualquier interrupción afecta tu operación inmediatamente."
        ),
        action_tpl="Buscá al menos un proveedor alternativo para tus productos clave.",
    ),
}


# ── Render ─────────────────────────────────────────────────────────────────────


def _fmt_ars(value: Decimal) -> str:
    """Format a Decimal as an ARS currency string without decimals."""
    return f"${int(value):,}".replace(",", ".")


def render_insight(
    risk_code: str,
    state: Any,
    result: Any,
) -> tuple[str, str, str]:
    """
    Render (title, description, action_text) for the given risk_code
    using real data from BusinessState and HealthScoreResult.

    Returns
    -------
    (title, description, action_text)

    Raises
    ------
    KeyError
        If risk_code is not in TEMPLATES.
    """
    template = TEMPLATES[risk_code]

    if risk_code == "CASH_LOW":
        gastos_fijos = state.monthly_fixed_expenses_est
        caja = state.cash_on_hand_est
        # days of coverage: cash / (fixed_expenses / 30)
        if gastos_fijos > 0:
            dias_cobertura = int(caja / (gastos_fijos / Decimal("30")))
        else:
            dias_cobertura = 0
        vars_: dict[str, str] = {
            "dias_cobertura": str(dias_cobertura),
            "caja_ars": _fmt_ars(caja),
            "gastos_fijos": _fmt_ars(gastos_fijos),
        }

    elif risk_code == "MARGIN_LOW":
        vertical = state.vertical_code
        sales = state.monthly_sales_est
        if sales > 0:
            margin_raw = float(
                (sales - state.monthly_inventory_cost_est - state.monthly_fixed_expenses_est)
                / sales
            )
        else:
            margin_raw = 0.0
        margin_pct = round(margin_raw * 100, 1)
        min_pct, max_pct = _MARGIN_RANGES.get(vertical, (20, 35))
        # impact: 5% price increase on monthly sales
        impacto = state.monthly_sales_est * Decimal("0.05")
        vars_ = {
            "vertical": _VERTICAL_NAMES.get(vertical, vertical),
            "margin_pct": str(margin_pct),
            "min": str(min_pct),
            "max": str(max_pct),
            "impacto_ars": _fmt_ars(impacto),
        }

    elif risk_code == "STOCK_CRITICAL":
        critical_products = [
            p for p in state.products
            if p.stock_units <= p.low_stock_threshold_units
        ]
        n = len(critical_products)
        names = [p.name for p in critical_products[:3]]
        lista = ", ".join(names) if names else "sin datos"
        vars_ = {
            "n_productos": str(n),
            "lista_productos_criticos": lista,
        }

    else:  # SUPPLIER_DEPENDENCY — no dynamic vars
        vars_ = {}

    title = template.title_tpl.format(**vars_)
    description = template.description_tpl.format(**vars_)
    action = template.action_tpl.format(**vars_)

    return title, description, action


# ── Severity helpers ───────────────────────────────────────────────────────────


def severity_from_score(score_total: int) -> str:
    """Map score_total to severity_code (F3-02 spec)."""
    if score_total >= 80:
        return "LOW"
    if score_total >= 60:
        return "MEDIUM"
    if score_total >= 30:
        return "HIGH"
    return "CRITICAL"
