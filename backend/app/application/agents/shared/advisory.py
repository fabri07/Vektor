"""advisory — el asesor de negocio de Véktor (intent `pedir_consejo`).

Convierte HECHOS (BusinessFact de FactsService.collect_for_advice) en CONSEJO
en lenguaje llano. Sonnet NUNCA calcula ni inventa números — recibe los hechos
ya computados y razona sobre ellos. Mismo patrón que data_query_narrator, con
un gate de honestidad propio (assess_data_sufficiency) porque un consejo
inventado le hace perder plata a un kiosquero.

DÓNDE ENCHUFA: el mapeo domain→hechos vive SOLO en
`FactsService.collect_for_advice()` — este módulo llama
`collect_for_advice(domain)` con el domain tal cual llega del chat (mismo
vocabulario que consulta_libre: ventas/caja/stock/gastos/proveedores/
clientes/marketing). No hay una segunda tabla de mapeo acá.

Registro de lenguaje: reusa `plain_language.REGISTER_SIMPLE` — el mismo que
usa alert_explainer. Si hay que ajustar el tono, se ajusta en un solo lugar.

Alcance de esta fase (F1+F3, decisión ratificada): SIN `analisis` de
stats_engine (dos fuentes de números en el mismo prompt sin garantía de
mismo período/dataset) y SIN contexto macro (bonus para una fase posterior,
fail-soft — el prompt funciona sin él).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from app.application.agents.shared.plain_language import REGISTER_SIMPLE
from app.application.agents.shared.schemas import (
    AgentRequest,
    AgentResponse,
    Confidence,
    LLMCall,
    RiskLevel,
    UsageSummary,
)
from app.application.security.prompt_defense import wrap_user_input
from app.application.services.facts_service import BusinessFact, Provenance

_MODEL = "claude-sonnet-4-6"
_MAX_TOKENS = 700
_MIN_CONFIDENCE = 0.6

Mode = Literal["grounded", "general", "empty"]


@dataclass(frozen=True)
class DataSufficiency:
    mode: Mode
    usable_facts: list[BusinessFact]
    reason: str


# ── GATE DE HONESTIDAD ────────────────────────────────────────────────────────
def assess_data_sufficiency(
    facts: Sequence[BusinessFact], *, min_confidence: float = _MIN_CONFIDENCE
) -> DataSufficiency:
    """Decide si hay datos para un consejo GROUNDED, uno GENERAL, o EMPTY.

    Esta es la línea que separa a Véktor de "ChatGPT para negocios": cuando no
    hay datos, lo DICE, en vez de inventar un diagnóstico con cara de autoridad.
    """
    usable = [
        f
        for f in facts
        if f.provenance == Provenance.REAL
        and f.value is not None
        and f.confidence >= min_confidence
    ]
    real_but_thin = [
        f
        for f in facts
        if f.provenance == Provenance.REAL
        and f.value is not None
        and f.confidence < min_confidence
    ]
    if usable:
        return DataSufficiency(mode="grounded", usable_facts=usable, reason="")
    if real_but_thin:
        return DataSufficiency(
            mode="general",
            usable_facts=real_but_thin,
            reason=(
                "Todavía tengo pocos datos tuyos cargados, así que esto es una "
                "idea general. Va a ser mucho más preciso cuando cargues más "
                "ventas y gastos."
            ),
        )
    return DataSufficiency(
        mode="empty",
        usable_facts=[],
        reason=(
            "Todavía no tengo datos tuyos para darte un consejo con tus "
            "números. Cargá algunas ventas y gastos y te ayudo con algo "
            "hecho a tu medida."
        ),
    )


# ── SERIALIZADOR — HECHOS → texto compacto para Sonnet ────────────────────────
def render_facts_for_llm(facts: Sequence[BusinessFact]) -> str:
    """Nunca filas crudas: cada línea es un hecho citable por su fact_id."""
    if not facts:
        return "(sin hechos disponibles)"
    lines: list[str] = []
    for f in facts:
        if f.value is None:
            val = "sin dato"
        elif f.unit in (None, "ratio"):
            val = f"{f.value}"
        else:
            val = f"{f.value} {f.unit}"
        extra: list[str] = []
        if f.comparison_value is not None:
            extra.append(f"antes: {f.comparison_value}")
        if f.variation_pct is not None:
            signo = "+" if f.variation_pct >= 0 else ""
            extra.append(f"cambio: {signo}{f.variation_pct}%")
        if f.severity and f.severity != "info":
            extra.append(f"alerta: {f.severity}")
        if f.confidence < 1.0:
            extra.append(f"confianza: {int(f.confidence * 100)}%")
        suffix = f"  [{'; '.join(extra)}]" if extra else ""
        lines.append(f"- ({f.fact_id}) {f.metric} en {f.period or 'actual'}: {val}{suffix}")
    return "\n".join(lines)


# ── EL CORAZÓN — system prompt del asesor ─────────────────────────────────────
def build_advisory_system_prompt(
    *, domain: str, mode: Mode, reason: str, facts_text: str, business_name: str
) -> str:
    honesty_block = (
        ""
        if mode == "grounded"
        else (
            f"\n## IMPORTANTE — datos limitados\n"
            f"No hay datos suficientes para un consejo basado en SUS números. "
            f'Empezá tu respuesta diciendo esto, con estas palabras o parecidas '
            f'y en tono tranquilo, sin que suene a error: "{reason}" '
            f"Después SÍ podés dar una idea general útil, pero dejá claro que "
            f"es general, no sacada de sus datos. NO inventes números que no "
            f"te pasé.\n"
        )
    )

    return f"""Sos el asesor de negocio de Véktor. Ayudás a dueños de negocios chicos en \
Argentina —kioscos, ferreterías, negocios de limpieza, decoración— a entender \
sus números y decidir mejor.

MUY IMPORTANTE sobre quién te lee: muchos no terminaron la escuela. No saben \
qué es un "margen" ni un "porcentaje de rentabilidad". Si les hablás con \
palabras de contador, los perdés. Tu éxito se mide en una sola cosa: **que la \
persona entienda y pueda hacer algo con lo que le dijiste.**

## Reglas que no se rompen nunca
1. **No inventás números.** Usás SOLO los hechos que te paso abajo. Si un \
dato no está, no te lo imaginás. Preferís decir "eso no lo tengo" antes que \
inventar.
2. **No calculás.** Los números ya vienen hechos y redondeados. Vos los \
explicás, no los recalculás.
3. **Aconsejás, no ordenás.** El negocio lo conoce la persona mejor que vos. \
Decís "podés probar", "una idea es", "yo miraría". Nunca "tenés que".
4. **Cada consejo se apoya en un dato real.** Cuando sugerís algo, decí de \
qué número tuyo salió.

## Cómo tenés que escribir
{REGISTER_SIMPLE}

## La forma de tu respuesta (seguí este orden, pero escribilo natural, sin \
títulos)
1. **Qué está pasando** — en plata concreta, no en porcentajes sueltos. Corto.
2. **Por qué** — una sola causa, contada como se la contarías a un amigo en \
el mostrador.
3. **Qué podés probar** — UNA sugerencia, marcada como sugerencia. Concreta \
y chica.
4. **Cómo sabés si funcionó** — un número fácil de mirar, en palabras simples.

Todo junto, corto, como una charla. Si en tres o cuatro frases alcanza, mejor.
{honesty_block}
## HECHOS DE ESTE NEGOCIO (lo único que sabés — no inventes fuera de esto)
Negocio: {wrap_user_input(business_name)}
Dominio de la consulta: {domain}

{facts_text}

Ahora respondé a la persona. Simple, honesto, sobre SUS números."""


# ── PIPELINE COMPARTIDO — llamado desde cada agente ───────────────────────────
async def handle_advice(
    *,
    request: AgentRequest,
    db: Any,
    client: Any,
    agent_name: str,
    domain: str,
    tenant_id: Any,
    business_name: str = "tu negocio",
) -> AgentResponse:
    """Pipeline único de advisory para todos los agentes: facts → gate → LLM.

    `domain` llega tal cual del chat (ventas/caja/stock/gastos/proveedores/
    clientes/marketing) y se pasa DIRECTO a `collect_for_advice` — el mapeo a
    los packs de hechos vive únicamente ahí.
    """
    from app.application.services.facts_provider import (  # noqa: PLC0415
        build_facts_service,
    )
    from app.application.services.facts_service import Period  # noqa: PLC0415

    period = Period.last_n_days(30)
    try:
        facts_service = await build_facts_service(db, tenant_id, period)
        facts = facts_service.collect_for_advice(str(tenant_id), domain, period)
    except Exception:
        return AgentResponse(
            request_id=request.request_id,
            agent_name=agent_name,
            status="success",
            risk_level=RiskLevel.LOW,
            requires_approval=False,
            confidence=Confidence.LOW,
            message=(
                "Justo ahora tuve un problema para leer tus datos y no puedo "
                "darte un consejo. Probá de nuevo en un ratito."
            ),
            result={"summary": "Consejo falló al leer datos.", "advisory": True},
        )

    suf = assess_data_sufficiency(facts)

    if suf.mode == "empty":
        # Gate no-invention: sin datos, sin LLM — mismo patrón que el resto
        # del chat (ej. income._handle_data_query sin ventas).
        return AgentResponse(
            request_id=request.request_id,
            agent_name=agent_name,
            status="success",
            risk_level=RiskLevel.LOW,
            requires_approval=False,
            confidence=Confidence.LOW,
            message=suf.reason,
            result={
                "summary": "Consejo sin datos suficientes.",
                "advisory": True,
                "advisory_domain": domain,
                "advisory_mode": suf.mode,
            },
        )

    system = build_advisory_system_prompt(
        domain=domain,
        mode=suf.mode,
        reason=suf.reason,
        facts_text=render_facts_for_llm(suf.usable_facts),
        business_name=business_name,
    )
    safe_question = wrap_user_input(request.message)

    try:
        response = await client.messages.create(
            model=_MODEL,
            max_tokens=_MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": f"Pregunta del dueño: {safe_question}"}],
        )
    except Exception:
        return AgentResponse(
            request_id=request.request_id,
            agent_name=agent_name,
            status="success",
            risk_level=RiskLevel.LOW,
            requires_approval=False,
            confidence=Confidence.LOW,
            message="Justo ahora no pude armar el consejo. Probá de nuevo en unos segundos.",
            result={"summary": "Consejo falló en la generación.", "advisory": True},
        )

    text = response.content[0].text.strip()
    llm_call = LLMCall(
        source="advisory_narrator",
        model=_MODEL,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )
    confidence = Confidence.HIGH if suf.mode == "grounded" else Confidence.MEDIUM

    return AgentResponse(
        request_id=request.request_id,
        agent_name=agent_name,
        status="success",
        risk_level=RiskLevel.LOW,
        requires_approval=False,
        confidence=confidence,
        message=text,
        result={
            "summary": "Consejo de negocio.",
            "advisory": True,
            "advisory_domain": domain,
            "advisory_mode": suf.mode,
        },
        usage=UsageSummary(calls=[llm_call]),
    )
