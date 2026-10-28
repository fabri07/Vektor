"""
Health Score Service.

Orchestrates: Business State Layer → Heuristic Engine → Score Persistence.
Scores are only recalculated when data changes (not on every request).
Every decision is logged to decision_audit_log.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from redis.asyncio import Redis

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.analytics_service import AnalyticsService
from app.domain.product import effective_threshold
from app.heuristics.health_engine import (
    FUENTES_CON_SALDO,
    HealthScoreResult,
    calculate_health_score,
    mide_caja_por_flujo,
)
from app.observability.logger import get_logger
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.score import HealthScoreSnapshot
from app.persistence.repositories.health_score_repository import HealthScoreRepository
from app.state.business_state_service import BusinessState, compute_business_state

logger = get_logger(__name__)

HEURISTIC_VERSION = "v2"


class _NullRedis:
    """Minimal async Redis stub used by synchronous score recalculation paths."""

    async def get(self, _key: str) -> None:
        return None

    async def set(
        self,
        _key: str,
        _value: str,
        *,
        nx: bool = False,
        ex: int | None = None,
    ) -> bool | None:
        return True if not nx else True

    async def aclose(self) -> None:
        return None


def _score_level(score_total: int) -> str:
    if score_total >= 80:
        return "excellent"
    if score_total >= 60:
        return "healthy"
    if score_total >= 30:
        return "warning"
    return "critical"


def _state_to_dict(state: BusinessState) -> dict[str, Any]:
    return {
        "snapshot_id": str(state.snapshot_id),
        "tenant_id": str(state.tenant_id),
        "vertical_code": state.vertical_code,
        "data_completeness_score": state.data_completeness_score,
        "confidence_level": state.confidence_level,
        "monthly_sales_est": str(state.monthly_sales_est),
        "monthly_inventory_cost_est": str(state.monthly_inventory_cost_est),
        "monthly_fixed_expenses_est": str(state.monthly_fixed_expenses_est),
        "cash_on_hand_est": str(state.cash_on_hand_est),
        "cash_source": getattr(state, "cash_source", "desconocido"),
        "liquid_inflow_est": str(getattr(state, "liquid_inflow_est", "0")),
        "liquid_outflow_est": str(getattr(state, "liquid_outflow_est", "0")),
        # M·2b-3: el margen tal como lo midió FactsService (lo lee /margin-history).
        "margin_status": state.margin_status,
        "fixed_expenses_observed": state.fixed_expenses_observed,
        "margin_reason": state.margin_reason,
        "margin_sales_base": str(state.margin_sales_base),
        "cogs_sold_est": None if state.cogs_sold_est is None else str(state.cogs_sold_est),
        "margin_gross_pct": state.margin_gross_pct,
        "cost_coverage_confirmed_pct": state.cost_coverage_confirmed_pct,
        "cost_coverage_total_pct": state.cost_coverage_total_pct,
        # M·3-1: de dónde sale el subscore (lo lee /margin-history).
        "margin_net_source": state.margin_net_source,
        "margin_net_pct": state.margin_net_pct,
        "margin_net_period": state.margin_net_period,
        "product_count": state.product_count,
        "supplier_count": state.supplier_count,
        "main_concern": state.main_concern,
        "products": [
            {
                "product_id": str(product.product_id),
                "name": product.name,
                "stock_units": product.stock_units,
                "low_stock_threshold_units": product.low_stock_threshold_units,
                "sale_price_ars": str(product.sale_price_ars),
            }
            for product in state.products
        ],
    }


def margin_explanation_for(state: BusinessState, result: HealthScoreResult) -> str:
    """El margen del subscore dicho con su estado (M·2b-3); nunca un 0 inventado."""
    if result.margin_net_ratio is None:
        motivo = state.margin_reason or "sin costo de lo vendido"
        if state.margin_net_source == "mes_cerrado":
            # Puede faltar por gastos o por un mes incompleto, no sólo por costo.
            return f"Sin margen neto del último mes cerrado ({motivo}); no entra en el total."
        return f"Sin costo suficiente para medir el margen ({motivo}); no entra en el total."
    if state.margin_net_source == "mes_cerrado":
        # M·3-1: el neto del mes cerrado (ingreso − costo vendido − gastos
        # operativos), el mismo número de la tarjeta y del chat.
        neto_mc = result.margin_net_ratio * 100
        estimado = " estimado" if result.margin_status == "estimado" else ""
        return (
            f"Margen neto{estimado} del último mes cerrado de {neto_mc:.1f}% "
            f"(ventas − costo de lo vendido − gastos operativos; cobertura de costo "
            f"{state.cost_coverage_total_pct}%)."
        )
    # «Después de fijos» y no «neto»: el margen neto de FactsService (M·3-1) es
    # el del mes cerrado con TODOS los gastos operativos, otra cuenta.
    neto = result.margin_net_ratio * 100
    bruto = state.margin_gross_pct
    if result.margin_status == "estimado":
        return (
            f"Margen después de gastos fijos estimado en {neto:.1f}% (bruto {bruto}%; "
            f"cobertura de costo confirmada {state.cost_coverage_confirmed_pct}%, "
            f"con estimaciones {state.cost_coverage_total_pct}%)."
        )
    return f"Margen después de gastos fijos de {neto:.1f}% (bruto {bruto}%)."


def _build_dimensions(
    state: BusinessState,
    result: HealthScoreResult,
) -> list[dict[str, str | None]]:
    if state.cash_source == "flujo":
        # Sin saldo medido (D-5): el 0 de `cash_on_hand_est` no es una caja vacía.
        cash_explanation = (
            f"Sin saldo de caja medido; se mide la cobertura del flujo líquido: "
            f"entraron {state.liquid_inflow_est:.2f} ARS y salieron "
            f"{state.liquid_outflow_est:.2f} ARS en 30 días."
        )
    elif mide_caja_por_flujo(state):
        # Con saldo y sin gastos fijos (D-10): no hay días; se mide el flujo.
        cash_explanation = (
            f"Sin gastos fijos cargados; se mide la cobertura del flujo líquido: "
            f"entraron {state.liquid_inflow_est:.2f} ARS y salieron "
            f"{state.liquid_outflow_est:.2f} ARS en 30 días."
        )
    elif state.cash_source not in FUENTES_CON_SALDO:
        cash_explanation = "Sin datos de caja; la caja no entra al score."
    elif state.monthly_fixed_expenses_est > 0:
        cash_ratio = state.cash_on_hand_est / state.monthly_fixed_expenses_est
        cash_explanation = (
            f"Caja estimada {state.cash_on_hand_est:.2f} ARS, "
            f"equivale a {cash_ratio:.2f}x de gastos fijos."
        )
    else:
        # D-10: sin gastos fijos no hay días de cobertura; la caja no puntúa.
        cash_explanation = "Sin gastos fijos cargados; la caja no entra al score."

    margin_explanation = margin_explanation_for(state, result)

    if state.products:
        low_stock_count = sum(
            1
            for product in state.products
            if product.stock_units <= effective_threshold(product.low_stock_threshold_units)
        )
        stock_explanation = (
            f"{low_stock_count} de {len(state.products)} productos están en stock bajo."
        )
    else:
        stock_explanation = "Sin catálogo cargado; score de stock neutral."

    supplier_explanation = (
        f"Base estimada de {state.supplier_count} proveedores activos."
        if state.supplier_count > 0
        else "Sin proveedores cargados; score de proveedores en mínimo."
    )

    # Growth explanation
    if state.prev_monthly_sales_est > 0:
        growth_pct = float(
            (state.monthly_sales_est - state.prev_monthly_sales_est)
            / state.prev_monthly_sales_est
            * 100
        )
        growth_explanation = f"Ventas {growth_pct:+.1f}% vs período anterior (30 días previos)."
    else:
        growth_explanation = (
            "Sin historial del período anterior; crecimiento calculado como neutro."
        )

    return [
        {
            "dimension": "cash",
            # Ausente (D-10/E0-B-6): sin valor ni peso, como el margen.
            "value": None if result.score_cash is None else str(result.score_cash),
            "weight": "0.00" if result.score_cash is None else "0.30",
            "weighted_value": (
                "0.000"
                if result.score_cash is None
                else f"{Decimal(result.score_cash) * Decimal('0.30'):.3f}"
            ),
            "explanation": cash_explanation,
        },
        {
            "dimension": "stock",
            "value": str(result.score_stock),
            "weight": "0.20",
            "weighted_value": f"{Decimal(result.score_stock) * Decimal('0.20'):.3f}",
            "explanation": stock_explanation,
        },
        {
            "dimension": "supplier",
            "value": str(result.score_supplier),
            "weight": "0.10",
            "weighted_value": f"{Decimal(result.score_supplier) * Decimal('0.10'):.3f}",
            "explanation": supplier_explanation,
        },
        {
            "dimension": "margin",
            # Ausente: sin valor ni peso (el motor lo excluyó del total).
            "value": None if result.score_margin is None else str(result.score_margin),
            "weight": "0.00" if result.score_margin is None else "0.20",
            "weighted_value": (
                "0.000"
                if result.score_margin is None
                else f"{Decimal(result.score_margin) * Decimal('0.20'):.3f}"
            ),
            "explanation": margin_explanation,
        },
        {
            "dimension": "growth",
            "value": str(result.score_growth),
            "weight": "0.20",
            "weighted_value": f"{Decimal(result.score_growth) * Decimal('0.20'):.3f}",
            "explanation": growth_explanation,
        },
    ]


class HealthScoreService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._score_repo = HealthScoreRepository(session)

    async def recalculate_for_tenant(
        self,
        tenant_id: uuid.UUID,
        triggered_by: str,
        actor_user_id: uuid.UUID | None = None,
    ) -> HealthScoreSnapshot:
        """
        Full recalculation pipeline:
        1. Build Business State (30-day window)
        2. Apply heuristic rules per vertical
        3. Compute composite score
        4. Persist snapshot
        5. Log decision to audit log
        """
        now = datetime.now(UTC)
        redis = _NullRedis()

        # ── 1. Business State Layer ───────────────────────────────────────────
        state = await compute_business_state(
            tenant_id, self._session, cast("Redis", redis)
        )

        # Se instancia SIEMPRE: el paso 5 lo usa para registrar el evento pase lo
        # que pase con el benchmark. Cuando vivía dentro de un `if`, un tenant con
        # override de margen llegaba al paso 5 con la variable sin asignar
        # (`UnboundLocalError`) y se quedaba sin recálculo.
        analytics_svc = AnalyticsService(self._session)

        # ── 2. Benchmark de margen ────────────────────────────────────────────
        # El del JSON del rubro, siempre (`None` = lo resuelve
        # `calculate_health_score`). El camino data-driven quedó desconectado del
        # scoring: su muestra contaba eventos de recálculo en vez de negocios
        # distintos. Y el objetivo de /settings/health-config tampoco entra (D-4):
        # es margen BRUTO por producto, y acá se puntúa el NETO; aplicarlo
        # convertía un neto sano del 12% de un kiosco en MARGIN_LOW contra 20%.

        # ── 3. Heuristic Engine ───────────────────────────────────────────────
        result = calculate_health_score(state, benchmark=None)
        dimensions = _build_dimensions(state, result)

        # ── 4. Persist snapshot ───────────────────────────────────────────────
        snapshot = HealthScoreSnapshot(
            tenant_id=tenant_id,
            total_score=Decimal(result.score_total),
            level=_score_level(result.score_total),
            dimensions=dimensions,
            triggered_by=triggered_by,
            snapshot_date=now,
            created_at=now,
            score_cash=result.score_cash,
            score_margin=result.score_margin,
            score_stock=result.score_stock,
            score_supplier=result.score_supplier,
            score_growth=result.score_growth,
            source_snapshot_id=state.snapshot_id,
            heuristic_version=HEURISTIC_VERSION,
            primary_risk_code=result.primary_risk_code,
            confidence_level=result.confidence_level,
            benchmark_provenance=result.benchmark_provenance.value,
            benchmark_confidence=result.benchmark_confidence,
            data_completeness_score=Decimal(str(result.data_completeness_score)),
            score_inputs_json=_state_to_dict(state),
        )
        await self._score_repo.save(snapshot)

        # ── 5. Analytics event anonimizado (data moat) ───────────────────────
        # `None`, no 0.0, cuando el ratio no se puede calcular: un negocio sin
        # ventas no tiene margen 0%, no tiene margen. El cero era una observación
        # fabricada que entraba a los percentiles del rubro como si fuera real.
        # M·2b-3: sólo un margen CONFIRMADO alimenta la muestra del rubro; uno
        # estimado es un supuesto del negocio, no una observación.
        # Y sólo con gastos fijos OBSERVADOS: con los declarados en el onboarding
        # una respuesta de formulario entraría como observación (invariante 5b).
        margin_ratio: float | None = (
            result.margin_net_ratio
            if result.margin_status == "confirmado"
            and state.fixed_expenses_observed
            # M·3-1: el neto del mes cerrado es otra definición; no se mezcla en
            # la muestra del rubro (``compute_margin_benchmark``) con «bruto − fijos».
            and state.margin_net_source != "mes_cerrado"
            else None
        )
        cash_ratio: float | None = None
        # Sólo con un saldo (D-5): en flujo el 0 entraría a la muestra del rubro
        # como una caja vacía observada.
        if state.cash_source in FUENTES_CON_SALDO and state.monthly_fixed_expenses_est > 0:
            cash_ratio = float(state.cash_on_hand_est / state.monthly_fixed_expenses_est)
        low_stock_pct = 0.0
        if state.products:
            below = sum(
                1
                for p in state.products
                if p.stock_units <= effective_threshold(p.low_stock_threshold_units)
            )
            low_stock_pct = below / len(state.products)

        await analytics_svc.record_score_event(
            vertical_code=state.vertical_code,
            score_total=result.score_total,
            score_cash=result.score_cash,
            score_margin=result.score_margin,
            score_stock=result.score_stock,
            score_supplier=result.score_supplier,
            margin_ratio=margin_ratio,
            cash_ratio=cash_ratio,
            supplier_count=state.supplier_count,
            product_count=state.product_count,
            low_stock_pct=low_stock_pct,
            data_completeness=state.data_completeness_score,
        )

        # ── 6. Audit log ──────────────────────────────────────────────────────
        audit = DecisionAuditLog(
            tenant_id=tenant_id,
            decision_type="health_score_recalculated",
            decision_data={
                "total_score": result.score_total,
                "level": snapshot.level,
                "snapshot_id": str(snapshot.id),
                "primary_risk_code": result.primary_risk_code,
            },
            triggered_by=triggered_by,
            actor_user_id=actor_user_id,
            created_at=now,
        )
        self._session.add(audit)
        await self._session.flush()

        logger.info(
            "health_score.recalculated",
            tenant_id=str(tenant_id),
            score=result.score_total,
            level=snapshot.level,
            triggered_by=triggered_by,
        )

        await redis.aclose()
        return snapshot
