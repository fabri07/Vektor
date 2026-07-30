"""AgentHealth — sub_narrator.

Genera la narrativa ejecutiva con LLM a partir de ComponentScoresV2.
El LLM recibe números ya calculados — NUNCA los recalcula.
Modelo: claude-sonnet-4-6 (más capaz que haiku para narrativa ejecutiva).
"""

from __future__ import annotations

from typing import Any

from app.application.agents.health.sub_calculator import ComponentScoresV2
from app.application.agents.shared.schemas import LLMCall
from app.application.security.prompt_defense import wrap_user_input

_SYSTEM = (
    "Sos un consultor financiero de Véktor. Generá una narrativa ejecutiva "
    "clara y accionable basada en los datos numéricos que te dan.\n\n"
    "REGLAS:\n"
    "- Usá los números dados, nunca los inventes.\n"
    "- Máximo 3 párrafos cortos.\n"
    "- Priorizá las alertas más urgentes.\n"
    "- No confundas correlación con causalidad.\n"
    "- Idioma: español argentino, tono directo y profesional.\n"
    "- Mencioná la dimensión de crecimiento solo si es relevante (positivo o negativo)."
)

_MODEL = "claude-sonnet-4-6"
_MAX_TOKENS = 800


def _build_prompt(scores: ComponentScoresV2, business_name: str) -> str:
    growth_label = (
        "sin historial" if scores.growth_score == 50 else f"{scores.growth_score:.0f}/100"
    )
    return (
        f"Negocio: {wrap_user_input(business_name)}\n"
        f"Score de salud: {scores.total_score}/100\n"
        f"- Caja: {scores.cash_score}/100\n"
        f"- Inventario: {scores.stock_score}/100\n"
        f"- Proveedores: {scores.supplier_score}/100\n"
        f"- Márgenes: {scores.margin_score}/100\n"
        f"- Crecimiento de ventas: {growth_label}\n"
        f"Riesgo principal: {scores.primary_risk_code}\n"
        # `confidence_level` dejó de ser solo la completitud de los datos: ahora
        # es la confianza EFECTIVA (datos ∧ procedencia del benchmark). El rótulo
        # viejo, "Confianza en los datos", le mentía al narrador sobre qué mide.
        f"Confianza del diagnóstico: {scores.confidence_level}"
    )


async def generate(
    scores: ComponentScoresV2,
    business_name: str,
    client: Any,
) -> tuple[str, LLMCall]:
    """Genera narrativa ejecutiva. Retorna (texto, LLMCall)."""
    prompt = _build_prompt(scores, business_name)
    response = await client.messages.create(
        model=_MODEL,
        max_tokens=_MAX_TOKENS,
        system=_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    llm_call = LLMCall(
        source="agent_health_narrator",
        model=_MODEL,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )
    return response.content[0].text.strip(), llm_call
