"""alert_explainer — explica el alert rojo del dashboard con datos reales.

Compone, no calcula:
  - El NÚMERO sale de FactsService (dashboard_snapshot + sobrestock), fresco
    en cada consulta — NUNCA se cachea (quedaría stale al cambiar datos).
  - El SIGNIFICADO funcional (qué es la métrica, por qué Véktor la marca) sale
    del catálogo determinístico _ALERT_MEANING de este módulo. Un test de
    paridad lo ata a los risk codes canónicos de heuristics/insight_templates
    (TEMPLATES): un risk code nuevo sin entrada acá rompe CI, no producción.
  - La REDACCIÓN la hace el LLM en lenguaje llano (registro compartido de
    plain_language.py). El LLM narra; no computa ni inventa.

Contrato de ids (verificado): el dashboard hoy expone `risk_codes` del health
engine (HealthAlertBanner usa `score.primary_risk_code`: CASH_LOW, MARGIN_LOW,
STOCK_CRITICAL, SUPPLIER_DEPENDENCY). Este módulo acepta ESOS ids y también
fact_ids de FactsService directos (forward-compatible para cuando el dashboard
lea BusinessFact).

Semántica de `still_alert`: para un risk_code el que manda es el health engine
— si el banner está en pantalla, la alerta ESTÁ activa (los dos sistemas de
severidad son intencionalmente distintos; ver CLAUDE.md). El severity del
BusinessFact solo decide `still_alert` para fact_ids directos de FactsService.
"""

from __future__ import annotations

from typing import Any

from app.application.agents.shared.plain_language import REGISTER_SIMPLE
from app.application.agents.shared.schemas import LLMCall
from app.application.security.prompt_defense import wrap_user_input
from app.application.services.facts_service import BusinessFact, FactsService, Period

_MODEL = "claude-sonnet-4-6"
_MAX_TOKENS = 600

# risk_code del health engine → métrica(s) de FactsService que lo fundamentan.
# SUPPLIER_DEPENDENCY no tiene BusinessFact todavía → se explica solo
# funcionalmente (y el caller loguea el gap para el backlog).
_RISK_CODE_TO_METRICS: dict[str, list[str]] = {
    "CASH_LOW": ["caja_liquida", "fiado_pendiente"],
    "MARGIN_LOW": ["margen_neto", "margen_bruto"],
    "STOCK_CRITICAL": ["valor_stock"],
    "SUPPLIER_DEPENDENCY": [],
}

# Significado funcional de cada alerta (fallback determinístico si el manual
# de AgentHelper no tiene entrada). Qué es y por qué Véktor lo marca.
_ALERT_MEANING: dict[str, str] = {
    "CASH_LOW": (
        "Caja baja: la plata líquida que entró (sin contar fiado ni tarjeta de "
        "crédito, que tarda ~30 días) viene floja. Véktor lo marca porque sin "
        "efectivo disponible cuesta reponer mercadería y pagar lo del día a día."
    ),
    "MARGIN_LOW": (
        "Margen bajo: de lo que se vende, después de pagar todo, queda poco. "
        "Véktor lo marca comparando contra lo sano para un negocio como este."
    ),
    "STOCK_CRITICAL": (
        "Stock crítico: hay productos importantes con pocas unidades o sin "
        "unidades. Véktor lo marca porque un quiebre de stock es venta perdida."
    ),
    "SUPPLIER_DEPENDENCY": (
        "Dependencia de proveedor: una parte grande de las compras se concentra "
        "en un solo proveedor. Véktor lo marca porque si ese proveedor falla o "
        "sube precios, pega directo en el negocio."
    ),
}


def resolve_alert_facts(
    facts_service: FactsService,
    tenant_id: str,
    alert_ids: list[str],
    period: Period,
    *,
    include_demo: bool = False,
) -> list[dict[str, Any]]:
    """Resuelve cada alert_id a su BusinessFact fresco + significado funcional.

    Acepta fact_ids directos y risk_codes del health engine. Ids desconocidos
    se saltan con gracia (no rompen la explicación de los demás). Si un fact_id
    directo ya no está en rojo (los datos cambiaron), se informa el estado
    actual igual — eso es honestidad, no un error.

    `include_demo`: los tenants demo tienen TODO con provenance='DEMO' y el
    health engine que pinta el banner no filtra provenance — pasar
    `tenant.is_demo` acá para que el chat vea los mismos números que el
    dashboard (si no, en las cuentas demo todos los facts salen EMPTY).

    El snapshot y el sobrestock se computan UNA vez para todos los alert_ids
    (get_alert_by_id recomputaría el snapshot completo por id).
    """
    snapshot = facts_service.dashboard_snapshot(
        tenant_id, period, include_demo=include_demo
    )
    sobres = facts_service.sobrestock(tenant_id, include_demo=include_demo)
    by_fact_id: dict[str, BusinessFact] = {
        f.fact_id: f for f in [*snapshot.values(), *sobres]
    }
    by_metric: dict[str, BusinessFact] = {f.metric: f for f in snapshot.values()}

    blocks: list[dict[str, Any]] = []
    for alert_id in alert_ids:
        fact = by_fact_id.get(alert_id)
        from_risk_code = False
        meaning = _ALERT_MEANING.get(alert_id)
        if fact is None and alert_id in _RISK_CODE_TO_METRICS:
            # risk_code del health engine → la métrica que lo fundamenta
            from_risk_code = True
            for metric in _RISK_CODE_TO_METRICS[alert_id]:
                candidate = by_metric.get(metric)
                if candidate is not None:
                    fact = candidate
                    break
        if fact is None and meaning is None:
            # Id desconocido: ni fact ni significado — se salta, no se inventa.
            continue
        # still_alert: para risk_codes manda el health engine (el banner está en
        # pantalla → la alerta está activa; los facts de caja/stock no modelan
        # ese severity). Para fact_ids directos decide el severity del fact.
        still_alert = (
            True
            if from_risk_code
            else bool(fact and fact.severity in ("warning", "critical"))
        )
        blocks.append(
            {
                "alert_id": alert_id,
                "fact": fact.model_dump() if fact is not None else None,
                "meaning": meaning,
                "still_alert": still_alert,
            }
        )
    return blocks


def _facts_block(blocks: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for b in blocks:
        fact = b.get("fact")
        if fact:
            val = (
                "sin dato"
                if fact["value"] is None
                else f"{fact['value']} {fact['unit'] or ''}".strip()
            )
            estado = (
                "EN ALERTA"
                if b["still_alert"]
                else "ya no está en alerta (los datos cambiaron)"
            )
            lines.append(
                f"- ({fact['fact_id']}) {fact['metric']} en {fact['period'] or 'actual'}: "
                f"{val} — estado: {estado}"
            )
        if b.get("meaning"):
            lines.append(f"  Qué significa: {b['meaning']}")
    return "\n".join(lines) if lines else "(sin datos para esta alerta)"


async def explain_alerts(
    question: str,
    blocks: list[dict[str, Any]],
    business_name: str,
    client: Any,
) -> tuple[str, LLMCall]:
    """Redacta la explicación del/los alert(s) en lenguaje llano. El LLM narra,
    no computa: los números vienen resueltos en `blocks` (FactsService)."""
    system = f"""Sos el asistente de Véktor. La persona está mirando su tablero y \
pregunta por un mensaje de alerta que ve en pantalla. Explicáselo con SUS \
números, en lenguaje llano.

## Reglas que no se rompen nunca
1. Usá ÚNICAMENTE los datos de abajo — cifras ya calculadas. NO inventes ni \
recalcules.
2. Citá el número real al explicar (la persona tiene que ver que hablás de SU \
negocio).
3. Si la alerta figura como "ya no está en alerta", decilo con franqueza: los \
datos cambiaron y hoy está mejor — explicá qué era lo que se marcaba.
4. Si para una alerta no hay número, explicá solo qué significa y sugerí qué \
cargar en Véktor para tener el dato.
5. Cerrá con UNA cosa concreta para mirar o probar, marcada como sugerencia \
("podés probar", "yo miraría").

## Cómo escribir
{REGISTER_SIMPLE}

## Alertas activas en su pantalla (lo único que sabés)
Negocio: {wrap_user_input(business_name)}
{_facts_block(blocks)}

Respondé corto, como una charla de mostrador. Sin títulos ni listas largas."""

    safe_question = wrap_user_input(question)
    response = await client.messages.create(
        model=_MODEL,
        max_tokens=_MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": f"Pregunta del dueño: {safe_question}"}],
    )
    llm_call = LLMCall(
        source="alert_explainer",
        model=_MODEL,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )
    return response.content[0].text.strip(), llm_call
