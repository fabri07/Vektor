"""Síntesis multi-agente — AgentCEO Stage 3.

Se invoca solo cuando plan.requires_synthesis=True y ninguna response tiene
status=requires_approval. Genera una respuesta unificada con Sonnet 4.5.
"""

from __future__ import annotations

from typing import Any

from app.application.agents.shared.schemas import AgentRequest, AgentResponse, AgentTeamPlan, LLMCall
from app.application.security.prompt_defense import wrap_user_input
from app.observability.logger import get_logger

logger = get_logger(__name__)


async def synthesize_team_results(
    plan: AgentTeamPlan,
    responses: list[AgentResponse],
    request: AgentRequest,
    business_name: str,
    client: Any,
) -> tuple[str, LLMCall]:
    """Genera una respuesta conversacional unificada para un plan multi-task exitoso.

    Parámetros:
        plan: el AgentTeamPlan ejecutado
        responses: respuestas de cada task, en el orden del plan
        request: request original del usuario
        business_name: nombre del negocio (para personalizar)
        client: cliente Anthropic async (del orchestrator)
    """
    results_text = "\n\n".join(
        f"[Tarea {i + 1} — {r.agent_name}]\n"
        f"{r.result.get('summary') or str(r.result)[:300]}"
        for i, r in enumerate(responses)
    )

    system = (
        f"Sos el asistente financiero de Véktor para {business_name}.\n"
        "Se ejecutaron varias operaciones. Generá una respuesta conversacional "
        "unificada que resuma los resultados de forma clara y accionable.\n"
        "Máximo 3 párrafos. Tono directo, español rioplatense. Sin markdown."
    )
    user_content = (
        f"Mensaje original del usuario: {wrap_user_input(request.message)}\n\n"
        f"Resultados de las operaciones:\n{results_text}"
    )

    response = await client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=600,
        system=system,
        messages=[{"role": "user", "content": user_content}],
    )

    call = LLMCall(
        source="ceo_synthesis",
        model="claude-sonnet-4-5",
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )
    text = (
        response.content[0].text.strip() if response.content else "Operaciones completadas."
    )
    return text, call
