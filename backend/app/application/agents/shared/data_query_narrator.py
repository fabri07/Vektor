"""data_query_narrator — narrador compartido para ANSWER_DATA_QUERY.

Los handlers de cada agente lo llaman con datos ya calculados (structured_data).
El LLM narra en lenguaje natural — NUNCA calcula montos ni inventa cifras.
Modelo: claude-sonnet-4-6.
Patrón: idéntico a health/sub_narrator.py y ceo/synthesis.py.
"""

from __future__ import annotations

import json
from typing import Any

from app.application.agents.shared.schemas import LLMCall
from app.application.security.prompt_defense import wrap_user_input

_SYSTEM = (
    "Sos el asistente de Véktor. Respondé la pregunta del dueño usando ÚNICAMENTE "
    "los datos provistos (structured_data). Son cifras ya calculadas — NO inventes "
    "ni recalcules. Si el dato para responder no está en structured_data, decílo con "
    "franqueza y sugerí qué cargar en Véktor para obtenerlo. "
    "Español de Argentina, conciso, sin tecnicismos."
)

_MODEL = "claude-sonnet-4-6"
_MAX_TOKENS = 600


async def answer_data_query(
    question: str,
    domain: str,
    structured_data: dict[str, Any],
    business_name: str,
    client: Any,
) -> tuple[str, LLMCall]:
    """Genera una respuesta en lenguaje natural a la pregunta del usuario.

    Parámetros:
        question: pregunta original del usuario (pasa por wrap_user_input antes del LLM)
        domain: dominio del negocio (clientes, ventas, gastos, stock, proveedores, caja, marketing)
        structured_data: datos determinísticos ya calculados por el agente (nunca calcular acá)
        business_name: nombre del negocio (para personalizar la respuesta)
        client: cliente Anthropic async (del orchestrator o del agente)

    Retorna:
        (texto_respuesta, LLMCall) — el texto ya es strip()ped.
    """
    safe_question = wrap_user_input(question)
    user_content = (
        f"Negocio: {business_name}\n"
        f"Dominio: {domain}\n"
        f"Pregunta del dueño: {safe_question}\n\n"
        f"Datos disponibles:\n"
        f"{json.dumps(structured_data, ensure_ascii=False, indent=2)}"
    )

    response = await client.messages.create(
        model=_MODEL,
        max_tokens=_MAX_TOKENS,
        system=_SYSTEM,
        messages=[{"role": "user", "content": user_content}],
    )

    llm_call = LLMCall(
        source="data_query_narrator",
        model=_MODEL,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )
    return response.content[0].text.strip(), llm_call
