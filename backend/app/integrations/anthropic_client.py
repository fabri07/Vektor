"""Anthropic client factory.

Keeps secret loading centralized so agents do not depend on shell-exported env vars.
"""

from collections.abc import Callable
from typing import Any, TypeVar

import anthropic

from app.config.settings import get_settings

ClientT = TypeVar("ClientT")


class AnthropicConfigurationError(RuntimeError):
    """Raised when Anthropic is required but no API key is configured."""


def _is_mock_factory(factory: Callable[..., Any]) -> bool:
    """Allow tests to inject patched constructors without requiring real secrets."""
    return factory.__class__.__module__.startswith("unittest.mock")


def get_anthropic_async_client(
    client_factory: Callable[..., ClientT] = anthropic.AsyncAnthropic,  # type: ignore[assignment]
) -> ClientT:
    settings = get_settings()
    api_key = settings.ANTHROPIC_API_KEY.strip()

    if not api_key:
        if _is_mock_factory(client_factory):
            return client_factory()
        raise AnthropicConfigurationError(
            "ANTHROPIC_API_KEY is not configured. Set it in backend/.env "
            "or in the deployment environment."
        )

    # timeout: acota cada llamada HTTP al LLM (F0.2).
    # max_retries=0: el retry transitorio lo maneja el TeamPlanExecutor (F0.4);
    # delegar al SDK mezclaría los dos niveles de retry.
    return client_factory(
        api_key=api_key,
        timeout=settings.ANTHROPIC_TIMEOUT_SECONDS,
        max_retries=0,
    )
