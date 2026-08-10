"""Tests de timeout y retry transitorio en TeamPlanExecutor — Task 2 (F0.2/F0.3/F0.4).

Cubre:
- F0.3: timeout por task (asyncio.wait_for) → TimeoutError como error transitorio.
- F0.4: retry distingue errores transitorios (OperationalError, TimeoutError)
         de errores de negocio (ValueError, RuntimeError).
- Tabla de _is_retryable para las clases de excepción del brief.
- Acciones externas (MCP) siguen reintentando (comportamiento legacy preservado).
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import sqlalchemy.exc

from app.application.agents.shared.schemas import (
    ActionType,
    AgentRequest,
    AgentResponse,
    AgentTask,
)
from app.application.services.team_plan_executor import (
    TeamPlanExecutor,
    _is_retryable,
)

EXECUTOR_MOD = "app.application.services.team_plan_executor"


# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_request() -> AgentRequest:
    return AgentRequest(
        user_id=str(uuid.uuid4()),
        business_id=str(uuid.uuid4()),
        message="test",
    )


def _make_task(
    *,
    agent: str = "agent_income",
    action_type: ActionType = ActionType.REGISTER_SALE,
    task_id: str | None = None,
) -> AgentTask:
    return AgentTask(
        task_id=task_id or str(uuid.uuid4()),
        agent=agent,
        action_type=action_type,
        entities={},
        depends_on=[],
    )


def _mock_session_cm() -> MagicMock:
    """Devuelve un async context manager que produce una sesión mock."""
    session = MagicMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _patch_session_factory():
    factory = MagicMock(side_effect=lambda: _mock_session_cm())
    return patch(f"{EXECUTOR_MOD}.async_session_factory", factory)


def _patch_settings(timeout: float = 5.0):
    """Parchea get_settings en el módulo del executor con el timeout indicado."""
    mock_settings = MagicMock()
    mock_settings.AGENT_TASK_TIMEOUT_SECONDS = timeout
    return patch(f"{EXECUTOR_MOD}.get_settings", return_value=mock_settings)


def _executor() -> TeamPlanExecutor:
    return TeamPlanExecutor(
        redis=MagicMock(),
        user_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
    )


def _success_resp(request: AgentRequest, agent: str = "agent_income") -> AgentResponse:
    return AgentResponse(
        request_id=request.request_id,
        agent_name=agent,
        status="success",
        risk_level="LOW",
        result={},
    )


# ── Tabla de _is_retryable ─────────────────────────────────────────────────────


class TestIsRetryable:
    """Verifica la tabla completa del brief: qué excepciones son reintentables."""

    def test_asyncio_timeout_error_retryable(self) -> None:
        assert _is_retryable(TimeoutError()) is True

    def test_anthropic_api_connection_error_retryable(self) -> None:
        import anthropic

        exc = anthropic.APIConnectionError(request=MagicMock())
        assert _is_retryable(exc) is True

    def test_anthropic_api_timeout_error_retryable(self) -> None:
        import anthropic

        exc = anthropic.APITimeoutError(request=MagicMock())
        assert _is_retryable(exc) is True

    def test_anthropic_internal_server_error_retryable(self) -> None:
        import anthropic

        exc = anthropic.InternalServerError(
            message="Internal Server Error",
            response=MagicMock(status_code=500),
            body=None,
        )
        assert _is_retryable(exc) is True

    def test_sqlalchemy_operational_error_retryable(self) -> None:
        exc = sqlalchemy.exc.OperationalError("select 1", {}, Exception("orig"))
        assert _is_retryable(exc) is True

    def test_sqlalchemy_dbapi_error_retryable(self) -> None:
        exc = sqlalchemy.exc.DBAPIError("select 1", {}, Exception("orig"))
        assert _is_retryable(exc) is True

    def test_value_error_not_retryable(self) -> None:
        assert _is_retryable(ValueError("monto inválido")) is False

    def test_runtime_error_not_retryable(self) -> None:
        assert _is_retryable(RuntimeError("boom")) is False

    def test_key_error_not_retryable(self) -> None:
        assert _is_retryable(KeyError("falta key")) is False

    def test_connection_error_not_retryable(self) -> None:
        # ConnectionError genérico de Python: NO es transitorio en el sentido del brief
        # (solo las específicas de anthropic/sqla lo son)
        assert _is_retryable(ConnectionError("generic")) is False

    def test_type_error_not_retryable(self) -> None:
        assert _is_retryable(TypeError("wrong type")) is False


# ── Timeout F0.3 ──────────────────────────────────────────────────────────────


class TestTimeoutBehavior:
    """Verifica que asyncio.wait_for acote cada llamada al agente."""

    async def test_timeout_triggers_retry_and_second_succeeds(self) -> None:
        """Primer intento supera el timeout → reintento → segundo intento ok → success."""
        request = _make_request()
        task = _make_task()
        call_count = 0

        async def slow_then_fast(*args: object, **kwargs: object) -> AgentResponse:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Duerme más que el timeout configurado → TimeoutError
                await asyncio.sleep(10)
            return _success_resp(request)

        mock_agent = MagicMock()
        mock_agent.process = slow_then_fast

        with (
            patch(f"{EXECUTOR_MOD}.get_sub_agent", return_value=mock_agent),
            _patch_session_factory(),
            _patch_settings(timeout=0.02),
            patch(f"{EXECUTOR_MOD}._RETRY_BACKOFF_SECONDS", 0),
        ):
            resp = await _executor()._run_task(task, request)

        assert resp.status == "success"
        assert call_count == 2

    async def test_timeout_exhausted_returns_error_with_timeout_key(self) -> None:
        """Ambos intentos superan el timeout → status=error con result['error']='timeout'."""
        request = _make_request()
        task = _make_task()
        call_count = 0

        async def always_slow(*args: object, **kwargs: object) -> AgentResponse:
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(10)
            raise RuntimeError("no debería llegar acá")  # inalcanzable

        mock_agent = MagicMock()
        mock_agent.process = always_slow

        with (
            patch(f"{EXECUTOR_MOD}.get_sub_agent", return_value=mock_agent),
            _patch_session_factory(),
            _patch_settings(timeout=0.02),
            patch(f"{EXECUTOR_MOD}._RETRY_BACKOFF_SECONDS", 0),
        ):
            resp = await _executor()._run_task(task, request)

        assert resp.status == "error"
        assert resp.result.get("error") == "timeout"
        assert call_count == 2

    async def test_timeout_does_not_hang_gather(self) -> None:
        """asyncio.gather NO cuelga si un agente interno hace timeout."""
        request = _make_request()
        task = _make_task()

        async def always_slow(*args: object, **kwargs: object) -> AgentResponse:
            await asyncio.sleep(60)
            raise RuntimeError("inalcanzable")

        mock_agent = MagicMock()
        mock_agent.process = always_slow

        with (
            patch(f"{EXECUTOR_MOD}.get_sub_agent", return_value=mock_agent),
            _patch_session_factory(),
            _patch_settings(timeout=0.02),
            patch(f"{EXECUTOR_MOD}._RETRY_BACKOFF_SECONDS", 0),
        ):
            # wait_for externo: si el executor cuelga, este test falla con TimeoutError
            resp = await asyncio.wait_for(
                _executor()._run_task(task, request),
                timeout=5.0,
            )

        assert resp.status == "error"
        assert resp.result.get("error") == "timeout"


# ── Retry de errores transitorios internos (F0.4) ────────────────────────────


class TestRetryableErrors:
    async def test_operational_error_retries_once_then_succeeds(self) -> None:
        """OperationalError de SQLAlchemy → reintento → éxito. process llamado 2 veces."""
        request = _make_request()
        task = _make_task()
        call_count = 0

        async def flaky(*args: object, **kwargs: object) -> AgentResponse:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise sqlalchemy.exc.OperationalError("connection lost", {}, Exception("orig"))
            return _success_resp(request)

        mock_agent = MagicMock()
        mock_agent.process = flaky

        with (
            patch(f"{EXECUTOR_MOD}.get_sub_agent", return_value=mock_agent),
            _patch_session_factory(),
            _patch_settings(),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            resp = await _executor()._run_task(task, request)

        assert resp.status == "success"
        assert call_count == 2

    async def test_value_error_not_retried(self) -> None:
        """ValueError (error de negocio) → sin reintento. process llamado 1 vez."""
        request = _make_request()
        task = _make_task()
        call_count = 0

        async def business_error(*args: object, **kwargs: object) -> AgentResponse:
            nonlocal call_count
            call_count += 1
            raise ValueError("monto inválido")

        mock_agent = MagicMock()
        mock_agent.process = business_error

        with (
            patch(f"{EXECUTOR_MOD}.get_sub_agent", return_value=mock_agent),
            _patch_session_factory(),
            _patch_settings(),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            resp = await _executor()._run_task(task, request)

        assert resp.status == "error"
        assert call_count == 1  # sin retry

    async def test_runtime_error_not_retried(self) -> None:
        """RuntimeError (error de negocio genérico) → sin reintento."""
        request = _make_request()
        task = _make_task()
        call_count = 0

        async def always_fails(*args: object, **kwargs: object) -> AgentResponse:
            nonlocal call_count
            call_count += 1
            raise RuntimeError("error interno")

        mock_agent = MagicMock()
        mock_agent.process = always_fails

        with (
            patch(f"{EXECUTOR_MOD}.get_sub_agent", return_value=mock_agent),
            _patch_session_factory(),
            _patch_settings(),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            resp = await _executor()._run_task(task, request)

        assert resp.status == "error"
        assert call_count == 1

    async def test_external_action_retries_on_connection_error(self) -> None:
        """Acción externa (MCP) reintenta con ConnectionError — comportamiento legacy."""
        request = _make_request()
        task = _make_task(
            agent="agent_google",
            action_type=ActionType.SYNC_TO_GOOGLE,
        )
        call_count = 0

        async def flaky_mcp(*args: object, **kwargs: object) -> AgentResponse:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("MCP caído")
            return AgentResponse(
                request_id=request.request_id,
                agent_name="agent_google",
                status="success",
                risk_level="LOW",
                result={},
            )

        mock_agent = MagicMock()
        mock_agent.process = flaky_mcp

        with (
            patch(f"{EXECUTOR_MOD}.get_sub_agent", return_value=mock_agent),
            _patch_session_factory(),
            _patch_settings(),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            resp = await _executor()._run_task(task, request)

        assert resp.status == "success"
        assert call_count == 2

    async def test_transient_error_capped_at_two_attempts(self) -> None:
        """Errores transitorios siempre tienen máximo 2 intentos (1 retry), nunca más."""
        request = _make_request()
        task = _make_task()
        call_count = 0

        async def always_operational_error(*args: object, **kwargs: object) -> AgentResponse:
            nonlocal call_count
            call_count += 1
            raise sqlalchemy.exc.OperationalError("siempre falla", {}, Exception("orig"))

        mock_agent = MagicMock()
        mock_agent.process = always_operational_error

        with (
            patch(f"{EXECUTOR_MOD}.get_sub_agent", return_value=mock_agent),
            _patch_session_factory(),
            _patch_settings(),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            resp = await _executor()._run_task(task, request)

        assert resp.status == "error"
        assert call_count == 2  # tope: 2 intentos (1 retry)
