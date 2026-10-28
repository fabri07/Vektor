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

from app.domain.product import effective_threshold
from app.domain.verticals import VERTICAL_LABELS, Vertical, parse_vertical
from app.heuristics.health_engine import (
    FUENTES_CON_SALDO,
    caja_sin_medir,
    margin_net_ratio,
    mide_caja_por_flujo,
)
from app.heuristics.verticals.loader import load_margin_benchmark


@dataclass(frozen=True)
class InsightTemplate:
    title_tpl: str
    description_tpl: str
    action_tpl: str


# ── Margin benchmarks (pct strings) per vertical ──────────────────────────────
# Sin default: un vertical fuera del catálogo levanta en parse_vertical antes de
# llegar acá. El nombre visible sale de VERTICAL_LABELS (app/domain/verticals.py).


def margin_range_pct(vertical: Vertical) -> tuple[int, int]:
    """Rango sano de margen del rubro, en puntos porcentuales, para la narrativa.

    Se LEE del JSON del vertical en vez de mantener una tabla paralela. La tabla
    que había acá repetía `healthy_min`/`healthy_max` con otros valores escritos
    a mano: recalibrar un rubro dejaba al texto del insight afirmando un rango
    que el score ya no usaba, y nada lo detectaba porque las dos fuentes nunca se
    comparaban entre sí.
    """
    benchmark = load_margin_benchmark(vertical)
    return round(benchmark.healthy_min * 100), round(benchmark.healthy_max * 100)


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
    "GROWTH_STAGNANT": InsightTemplate(
        title_tpl="Tus ventas {titulo_variacion}",
        description_tpl=(
            "En los últimos 30 días estimamos ventas por {ventas_ars}, contra "
            "{ventas_prev_ars} de los 30 días anteriores ({variacion_pct}%)."
        ),
        action_tpl=(
            "Mirá qué productos dejaron de moverse y si cambió algo en precios, "
            "horarios o reposición."
        ),
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
    description_override: str | None = None
    title_override: str | None = None
    action_override: str | None = None

    if risk_code == "CASH_LOW" and caja_sin_medir(state):
        # El snapshot marcó CASH_LOW pero el estado de hoy no permite medir la
        # caja (V-23): ni «cubre poco», ni días; se pide lo que falta.
        vars_: dict[str, str] = {}
        title_override = "Hoy no podemos medir tu caja"
        cash_source = getattr(state, "cash_source", None)
        if cash_source in FUENTES_CON_SALDO:
            description_override = (
                "No tenés gastos fijos cargados ni registramos salidas en efectivo, "
                "transferencia, QR o débito en los últimos 30 días: sin eso no podemos "
                "decir para cuánto alcanza tu caja."
            )
            action_override = "Cargá tus gastos fijos o los pagos y gastos que hiciste."
        elif cash_source == "flujo":
            entradas = Decimal(str(state.liquid_inflow_est))
            description_override = (
                f"En los últimos 30 días entraron {_fmt_ars(entradas)} en efectivo, "
                "transferencia, QR y débito, pero no registramos salidas: sin ellas no "
                "podemos medir tu caja."
            )
            action_override = "Cargá los pagos y gastos que hiciste, o hacé el arqueo del día."
        else:
            description_override = "No tenemos un saldo de caja ni movimientos para medirla."
            action_override = "Hacé el arqueo del día o cargá tus ventas y gastos."

    elif risk_code == "CASH_LOW" and (
        getattr(state, "cash_source", None) not in FUENTES_CON_SALDO or mide_caja_por_flujo(state)
    ):
        # Modo flujo (D-5): no hay saldo medido, así que no se afirma «$0 en
        # caja» ni una cobertura en días; se dice lo que se midió. También con
        # saldo y sin gastos fijos (D-10): sin fijos no hay días que contar.
        entradas = Decimal(str(state.liquid_inflow_est))
        salidas = Decimal(str(state.liquid_outflow_est))
        vars_ = {}
        title_override = "Tu plata líquida cubre poco lo que sale"
        cierre = (
            # Hay saldo, pero sin fijos no hay días de cobertura que afirmar.
            "Sin gastos fijos cargados no podemos decir para cuántos días alcanza "
            "tu caja: cargalos para saberlo."
            if getattr(state, "cash_source", None) in FUENTES_CON_SALDO
            else "No tenemos un saldo de caja medido: hacé el arqueo del día para saberlo."
        )
        description_override = (
            f"En los últimos 30 días entraron {_fmt_ars(entradas)} y salieron "
            f"{_fmt_ars(salidas)} en efectivo, transferencia, QR y débito. {cierre}"
        )

    elif risk_code == "CASH_LOW":
        gastos_fijos = state.monthly_fixed_expenses_est
        caja = state.cash_on_hand_est
        # days of coverage: cash / (fixed_expenses / 30)
        dias_cobertura = int(caja / (gastos_fijos / Decimal("30"))) if gastos_fijos > 0 else 0
        vars_ = {
            "dias_cobertura": str(dias_cobertura),
            "caja_ars": _fmt_ars(caja),
            "gastos_fijos": _fmt_ars(gastos_fijos),
        }
        # Severity-aware description variants — gated por score_cash del snapshot.
        # Sin `or` (E0-B-5): un 0 es la peor caja, no la mejor; un `None` (caja
        # sin medir) no elige variante.
        score_cash_val = getattr(result, "score_cash", None)
        if score_cash_val is not None and int(score_cash_val) < 30:
            description_override = (
                f"Situación crítica: con {_fmt_ars(caja)} en caja y "
                f"{_fmt_ars(gastos_fijos)} de gastos fijos mensuales, "
                f"tu cobertura operativa es de solo {dias_cobertura} días. "
                "El riesgo de no poder afrontar pagos es inmediato."
            )
        elif score_cash_val is not None and int(score_cash_val) < 60:
            description_override = (
                f"Con {_fmt_ars(caja)} en caja y {_fmt_ars(gastos_fijos)} de gastos fijos "
                f"mensuales, tenés cobertura para {dias_cobertura} días. Margen muy ajustado."
            )

    elif risk_code == "MARGIN_LOW":
        # El margen del subscore (M·2b-3): costo VENDIDO de FactsService, no la
        # mercadería comprada. «Estimamos» sólo si el margen es estimado.
        vertical = parse_vertical(state.vertical_code)
        ratio = margin_net_ratio(state)
        min_pct, max_pct = margin_range_pct(vertical)
        sales = state.margin_sales_base
        # impact: 5% price increase on monthly sales
        impacto = sales * Decimal("0.05")
        margin_pct = "" if ratio is None else str(round(ratio * 100, 1))
        vars_ = {
            "vertical": VERTICAL_LABELS[vertical],
            "margin_pct": margin_pct,
            "min": str(min_pct),
            "max": str(max_pct),
            "impacto_ars": _fmt_ars(impacto),
        }
        rango = f"El rango saludable para tu rubro es {min_pct}% – {max_pct}%."
        if ratio is None:
            # El snapshot marcó MARGIN_LOW pero el estado de hoy ya no tiene costo
            # suficiente: se dice eso, nunca un 0%.
            motivo = state.margin_reason or "sin costo de lo vendido"
            description_override = f"Hoy no podemos medir tu margen ({motivo}). {rango}"
        elif state.margin_status != "estimado":
            description_override = f"Tu margen es de {margin_pct}%. {rango}"

    elif risk_code == "STOCK_CRITICAL":
        critical_products = [
            p
            for p in state.products
            if p.stock_units <= effective_threshold(p.low_stock_threshold_units)
        ]
        n = len(critical_products)
        names = [p.name for p in critical_products[:3]]
        lista = ", ".join(names) if names else "sin datos"
        vars_ = {
            "n_productos": str(n),
            "lista_productos_criticos": lista,
        }

    elif risk_code == "GROWTH_STAGNANT":
        # Sólo con período anterior: sin historial el health engine no lo elige
        # como riesgo. El título dice lo que pasó, nunca «caída» si no cayó.
        actual = Decimal(str(state.monthly_sales_est))
        previo = Decimal(str(state.prev_monthly_sales_est))
        variacion = (actual - previo) / previo * 100 if previo > 0 else None
        def _pct(v: Decimal) -> str:
            return str(round(v, 1)).replace(".", ",")

        if variacion is None:
            titulo = "no tienen un período anterior con qué compararse"
        elif variacion <= -10:
            titulo = f"cayeron {_pct(abs(variacion))}% contra los 30 días anteriores"
        elif variacion < 5:
            titulo = "están estancadas contra los 30 días anteriores"
        else:
            titulo = f"crecieron {_pct(variacion)}%, menos que el resto de tu salud"
        vars_ = {
            "titulo_variacion": titulo,
            "ventas_ars": _fmt_ars(actual),
            "ventas_prev_ars": _fmt_ars(previo),
            "variacion_pct": (
                "" if variacion is None else ("+" if variacion > 0 else "") + _pct(variacion)
            ),
        }
        if variacion is None:
            description_override = (
                f"En los últimos 30 días estimamos ventas por {_fmt_ars(actual)}. "
                "Todavía no hay 30 días anteriores para comparar."
            )

    else:  # SUPPLIER_DEPENDENCY — no dynamic vars
        vars_ = {}

    title = title_override if title_override is not None else template.title_tpl.format(**vars_)
    description = (
        description_override
        if description_override is not None
        else template.description_tpl.format(**vars_)
    )
    action = (
        action_override if action_override is not None else template.action_tpl.format(**vars_)
    )

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
