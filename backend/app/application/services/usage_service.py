"""
Servicio del dashboard de consumo de tokens / costo estimado (SUPERADMIN).

Determinístico, sin LLM. Lee filas de ``decision_audit_log`` (los tokens ya están
persistidos por cada llamada a un agente) y agrega en Python por modelo, agente,
día y tenant. Los costos se estiman con ``app.domain.model_pricing``.

⚠️ ESCALA: este servicio carga en memoria todas las filas con ``tokens_total > 0``
del rango pedido (default 30 días). Para el volumen actual (un puñado de llamadas
por chat) es perfectamente eficiente. Si el volumen crece materialmente, mover la
agregación a SQL (GROUP BY + SUM sobre columnas) o a una materialized view /
tabla de rollup diario — la forma del response no cambia.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.model_pricing import estimate_cost_usd
from app.persistence.models.audit import DecisionAuditLog
from app.schemas.admin_usage import (
    AgentUsage,
    DayUsage,
    ModelUsage,
    TenantUsage,
    UsageDashboardResponse,
    UsageTotals,
)

_UNKNOWN_MODEL = "unknown"
_UNKNOWN_AGENT = "unknown"


def _agent_name(decision_data: dict[str, Any]) -> str:
    """Agente a nivel fila (fallback): sub_agent_name → ceo_target_agent → unknown.

    Solo se usa cuando la fila no tiene `token_calls` con `source` por llamada
    (el `source` por call es la atribución real; `sub_agent_name` suele ser el
    orquestador/CEO y colapsaría todo el gasto sobre él).
    """
    name = decision_data.get("sub_agent_name") or decision_data.get("ceo_target_agent")
    if isinstance(name, str) and name.strip():
        return name
    return _UNKNOWN_AGENT


def _coerce_int(value: Any) -> int:
    """Coacciona a int de forma segura — datos JSONB no confiables no deben 500ear."""
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


async def get_usage_dashboard(
    session: AsyncSession,
    *,
    days: int,
    tenant_id: UUID | None,
) -> UsageDashboardResponse:
    """
    Agrega el consumo de tokens y el costo estimado en USD del rango ``[from, to]``.

    - ``to_date`` = hoy (UTC); ``from_date`` = hoy - (days-1).
    - Si ``tenant_id`` viene, filtra a ese tenant (el SUPERADMIN ve global por defecto).
    - Solo considera filas con ``tokens_total > 0`` (las de 0 no aportan costo).
    """
    today = datetime.now(UTC).date()
    from_date = today - timedelta(days=days - 1)
    to_date = today
    # Límite inferior inclusivo desde el comienzo del día from_date (UTC).
    from_dt = datetime.combine(from_date, time.min, tzinfo=UTC)

    stmt = (
        select(
            DecisionAuditLog.tenant_id,
            DecisionAuditLog.created_at,
            DecisionAuditLog.tokens_input,
            DecisionAuditLog.tokens_output,
            DecisionAuditLog.tokens_total,
            DecisionAuditLog.decision_data,
        )
        .where(DecisionAuditLog.created_at >= from_dt)
        .where(DecisionAuditLog.tokens_total > 0)
    )
    if tenant_id is not None:
        stmt = stmt.where(DecisionAuditLog.tenant_id == tenant_id)

    rows = (await session.execute(stmt)).all()

    # Acumuladores.
    tot_in = 0
    tot_out = 0
    tot_total = 0
    tot_cost = Decimal("0")
    tot_unpriced = 0  # tokens sin precio (modelo no mapeado o fila sin token_calls)
    decisions = 0

    # by_model: input/output/cost/priced
    model_in: dict[str, int] = defaultdict(int)
    model_out: dict[str, int] = defaultdict(int)
    model_cost: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    model_priced: dict[str, bool] = defaultdict(lambda: True)

    # by_agent / by_day / by_tenant: tokens_total + cost
    agent_tokens: dict[str, int] = defaultdict(int)
    agent_cost: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    day_tokens: dict[date, int] = defaultdict(int)
    day_cost: dict[date, Decimal] = defaultdict(lambda: Decimal("0"))
    tenant_tokens: dict[UUID, int] = defaultdict(int)
    tenant_cost: dict[UUID, Decimal] = defaultdict(lambda: Decimal("0"))

    for row in rows:
        decisions += 1
        row_tenant: UUID = row.tenant_id
        row_day: date = row.created_at.date()
        decision_data: dict[str, Any] = row.decision_data or {}

        tot_in += row.tokens_input
        tot_out += row.tokens_output
        tot_total += row.tokens_total

        # Costo de la fila: suma sobre token_calls (granularidad por modelo + source).
        token_calls = decision_data.get("token_calls")
        row_cost = Decimal("0")

        if isinstance(token_calls, list) and token_calls:
            fallback_agent = _agent_name(decision_data)
            for call in token_calls:
                if not isinstance(call, dict):
                    continue
                model = call.get("model") or _UNKNOWN_MODEL
                # Atribución REAL por llamada: el `source` del LLMCall (ceo, agent_health,
                # agent_stock, …). Fallback al agente de la fila si no viene.
                source = call.get("source")
                call_agent = (
                    source if isinstance(source, str) and source.strip() else fallback_agent
                )
                in_tok = _coerce_int(call.get("input_tokens"))
                out_tok = _coerce_int(call.get("output_tokens"))
                call_tokens = in_tok + out_tok
                cost, priced = estimate_cost_usd(model, in_tok, out_tok)
                model_in[model] += in_tok
                model_out[model] += out_tok
                model_cost[model] += cost
                # priced=False si CUALQUIER muestra de ese modelo no tiene precio.
                model_priced[model] = model_priced[model] and priced
                row_cost += cost
                if not priced:
                    tot_unpriced += call_tokens
                # by_agent por source real (no por sub_agent_name = orquestador).
                agent_tokens[call_agent] += call_tokens
                agent_cost[call_agent] += cost
        else:
            # Fila con tokens pero sin desglose de calls → modelo "unknown", costo 0.
            model_in[_UNKNOWN_MODEL] += row.tokens_input
            model_out[_UNKNOWN_MODEL] += row.tokens_output
            model_priced[_UNKNOWN_MODEL] = False
            tot_unpriced += row.tokens_total
            # Sin desglose por call → atribución a nivel fila (mejor esfuerzo), costo 0.
            agent_tokens[_agent_name(decision_data)] += row.tokens_total

        tot_cost += row_cost

        day_tokens[row_day] += row.tokens_total
        day_cost[row_day] += row_cost

        tenant_tokens[row_tenant] += row.tokens_total
        tenant_cost[row_tenant] += row_cost

    totals = UsageTotals(
        tokens_input=tot_in,
        tokens_output=tot_out,
        tokens_total=tot_total,
        cost_usd=float(tot_cost),
        decisions=decisions,
        unpriced_tokens=tot_unpriced,
    )

    by_model = sorted(
        (
            ModelUsage(
                model=model,
                tokens_input=model_in[model],
                tokens_output=model_out[model],
                cost_usd=float(model_cost[model]),
                priced=model_priced[model],
            )
            for model in model_in
        ),
        key=lambda m: m.cost_usd,
        reverse=True,
    )

    by_agent = sorted(
        (
            AgentUsage(
                agent=agent,
                tokens_total=agent_tokens[agent],
                cost_usd=float(agent_cost[agent]),
            )
            for agent in agent_tokens
        ),
        key=lambda a: a.cost_usd,
        reverse=True,
    )

    by_day = sorted(
        (
            DayUsage(
                date=day,
                tokens_total=day_tokens[day],
                cost_usd=float(day_cost[day]),
            )
            for day in day_tokens
        ),
        key=lambda d: d.date,
    )

    by_tenant = sorted(
        (
            TenantUsage(
                tenant_id=tid,
                tokens_total=tenant_tokens[tid],
                cost_usd=float(tenant_cost[tid]),
            )
            for tid in tenant_tokens
        ),
        key=lambda t: t.cost_usd,
        reverse=True,
    )

    return UsageDashboardResponse(
        days=days,
        from_date=from_date,
        to_date=to_date,
        totals=totals,
        by_agent=by_agent,
        by_model=by_model,
        by_day=by_day,
        by_tenant=by_tenant,
    )
