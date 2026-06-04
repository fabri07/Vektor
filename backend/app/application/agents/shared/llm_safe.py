"""Helper centralizado para llamadas LLM con manejo de errores tipado.

Uso:
    text, llm_call = await call_llm(
        client=self.client,
        source="agent_supplier",
        model="claude-sonnet-4-6",
        system=system,
        messages=[...],
        max_tokens=600,
    )
    if text is None:
        return fallback_result, None  # error ya logueado
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import anthropic

from app.application.agents.shared.schemas import LLMCall

if TYPE_CHECKING:
    from anthropic.types import MessageParam
from app.integrations.anthropic_client import AnthropicConfigurationError
from app.observability.logger import get_logger

logger = get_logger(__name__)


async def call_llm(
    *,
    client: anthropic.AsyncAnthropic,
    source: str,
    model: str,
    system: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
) -> tuple[str | None, LLMCall | None]:
    """Llama al LLM y devuelve (texto_respuesta, llm_call).

    Siempre devuelve (None, None) ante errores transitorios (rate limit,
    conexión, servidor), logueando el tipo específico para observabilidad.
    Re-lanza AnthropicConfigurationError para que el orquestador lo capture
    como error operacional y no como falla silenciosa de sub-agente.
    """
    try:
        response = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=cast("list[MessageParam]", messages),
        )
        llm_call = LLMCall(
            source=source,
            model=model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        # getattr (no isinstance) para tolerar bloques no-texto y mocks de tests
        raw_text = getattr(response.content[0], "text", "") if response.content else ""
        text = raw_text.strip() if isinstance(raw_text, str) else ""
        return text, llm_call
    except AnthropicConfigurationError:
        raise  # error de configuración → visible en el orquestador
    except anthropic.RateLimitError as exc:
        logger.warning("llm_rate_limited", source=source, error=str(exc))
    except anthropic.APIConnectionError as exc:
        logger.warning("llm_connection_error", source=source, error=str(exc))
    except anthropic.APITimeoutError as exc:
        logger.warning("llm_timeout", source=source, error=str(exc))
    except anthropic.InternalServerError as exc:
        logger.warning("llm_server_error", source=source, error=str(exc))
    except Exception as exc:
        logger.warning(
            "llm_unexpected_error",
            source=source,
            error=str(exc),
            error_type=type(exc).__name__,
        )
    return None, None
