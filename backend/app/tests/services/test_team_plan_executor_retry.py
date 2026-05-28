"""Tests de retry en TeamPlanExecutor para tasks externas — Stage 5c."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.application.agents.shared.schemas import (
    ActionType,
    AgentRequest,
    AgentResponse,
    AgentTask,
    AgentTeamPlan,
)
from app.application.services.team_plan_executor import TeamPlanExecutor, _EXTERNAL_ACTION_TYPES


def _make_task(action_type: str, agent: str = "agent_google") -> AgentTask:
    return AgentTask(
        task_id=str(uuid.uuid4()),
        agent=agent,
        action_type=ActionType(action_type),
    )


def _make_request() -> AgentRequest:
    return AgentRequest(
        user_id=str(uuid.uuid4()),
        business_id=str(uuid.uuid4()),
        message="test",
    )


def _success_response(request: AgentRequest, agent: str) -> AgentResponse:
    return AgentResponse(
        request_id=request.request_id,
        agent_name=agent,
        status="success",
        risk_level="LOW",
        result={},
    )


class TestExternalActionTypes:
    def test_google_actions_are_external(self) -> None:
        for at in (
            "SYNC_TO_GOOGLE",
            "CREATE_CALENDAR_EVENT",
            "CREATE_SUPPLIER_DRAFT",
            "UPLOAD_TO_DRIVE",
            "CREATE_GOOGLE_DOC",
            "APPEND_TO_SHEET",
        ):
            assert at in _EXTERNAL_ACTION_TYPES, f"{at} should be external"

    def test_internal_actions_not_external(self) -> None:
        for at in ("REGISTER_SALE", "REGISTER_EXPENSE", "UPDATE_STOCK"):
            assert at not in _EXTERNAL_ACTION_TYPES, f"{at} should not be external"


class TestRetryOnExternalTask:
    @pytest.mark.asyncio
    async def test_external_task_retries_on_exception(self) -> None:
        """Task externa falla en intento 1 y tiene éxito en intento 2."""
        executor = TeamPlanExecutor(
            redis=MagicMock(),
            user_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
        )
        request = _make_request()
        task = _make_task("SYNC_TO_GOOGLE")

        mock_agent = MagicMock()
        call_count = 0

        async def flaky_process(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("transient network error")
            return _success_response(request, "agent_google")

        mock_agent.process = flaky_process

        with patch(
            "app.application.services.team_plan_executor.get_sub_agent",
            return_value=mock_agent,
        ), patch(
            "app.application.services.team_plan_executor.async_session_factory",
        ) as mock_factory:
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
            with patch("asyncio.sleep", new=AsyncMock()):
                resp = await executor._run_task(task, request)

        assert resp.status == "success"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_internal_task_does_not_retry(self) -> None:
        """Task interna (REGISTER_SALE) no reintenta — falla directo."""
        executor = TeamPlanExecutor(
            redis=MagicMock(),
            user_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
        )
        request = _make_request()
        task = _make_task("REGISTER_SALE", agent="agent_income")

        mock_agent = MagicMock()
        call_count = 0

        async def always_fails(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise ValueError("logic error")

        mock_agent.process = always_fails

        with patch(
            "app.application.services.team_plan_executor.get_sub_agent",
            return_value=mock_agent,
        ), patch(
            "app.application.services.team_plan_executor.async_session_factory",
        ) as mock_factory:
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
            with patch("asyncio.sleep", new=AsyncMock()):
                resp = await executor._run_task(task, request)

        assert resp.status == "error"
        assert call_count == 1  # sin retry

    @pytest.mark.asyncio
    async def test_external_task_exhausted_returns_error(self) -> None:
        """Task externa falla en ambos intentos → status=error."""
        executor = TeamPlanExecutor(
            redis=MagicMock(),
            user_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
        )
        request = _make_request()
        task = _make_task("UPLOAD_TO_DRIVE")

        mock_agent = MagicMock()
        mock_agent.process = AsyncMock(side_effect=ConnectionError("always fails"))

        with patch(
            "app.application.services.team_plan_executor.get_sub_agent",
            return_value=mock_agent,
        ), patch(
            "app.application.services.team_plan_executor.async_session_factory",
        ) as mock_factory:
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
            with patch("asyncio.sleep", new=AsyncMock()):
                resp = await executor._run_task(task, request)

        assert resp.status == "error"
        assert mock_agent.process.call_count == 2  # 2 intentos antes de rendirse
