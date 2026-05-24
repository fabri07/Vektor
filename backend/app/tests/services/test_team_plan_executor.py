"""Tests del TeamPlanExecutor — DAG real, paralelización, sesiones aisladas,
skip downstream en fallo, propagación de context N→N+1, orden de respuestas.

Mockeamos `get_sub_agent` y `async_session_factory` para evitar la DB real.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.application.agents.shared.schemas import (
    ActionType,
    AgentRequest,
    AgentResponse,
    AgentTask,
    AgentTeamPlan,
    RiskLevel,
)
from app.application.services.team_plan_executor import (
    TeamPlanExecutor,
    _topological_levels,
)

EXECUTOR_MOD = "app.application.services.team_plan_executor"


# ── Helpers ───────────────────────────────────────────────────────────────────


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
    depends_on: list[str] | None = None,
    task_id: str | None = None,
) -> AgentTask:
    return AgentTask(
        task_id=task_id or str(uuid.uuid4()),
        agent=agent,
        action_type=action_type,
        entities={},
        depends_on=depends_on or [],
    )


def _make_response(
    request_id: str,
    *,
    agent_name: str = "agent_income",
    status: str = "success",
    summary: str = "ok",
) -> AgentResponse:
    return AgentResponse(
        request_id=request_id,
        agent_name=agent_name,
        status=status,
        risk_level=RiskLevel.LOW,
        result={"summary": summary},
    )


def _mock_session_cm() -> MagicMock:
    """Devuelve un async context manager que produce una sesión mock."""
    session = MagicMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _patch_session_factory():
    """Patchea async_session_factory para que retorne un context manager mockeado."""
    factory = MagicMock(side_effect=lambda: _mock_session_cm())
    return patch(f"{EXECUTOR_MOD}.async_session_factory", factory)


# ── _topological_levels ───────────────────────────────────────────────────────


def test_topological_levels_no_deps_all_parallel():
    """Sin dependencias → todas las tasks en el mismo nivel (paralelo)."""
    t1 = _make_task(task_id="t1")
    t2 = _make_task(task_id="t2")
    t3 = _make_task(task_id="t3")
    levels = _topological_levels([t1, t2, t3])
    assert len(levels) == 1
    assert {t.task_id for t in levels[0]} == {"t1", "t2", "t3"}


def test_topological_levels_sequential_chain():
    """t1 → t2 → t3: tres niveles, uno por task."""
    t1 = _make_task(task_id="t1")
    t2 = _make_task(task_id="t2", depends_on=["t1"])
    t3 = _make_task(task_id="t3", depends_on=["t2"])
    levels = _topological_levels([t1, t2, t3])
    assert len(levels) == 3
    assert [lv[0].task_id for lv in levels] == ["t1", "t2", "t3"]


def test_topological_levels_diamond():
    """t1 → (t2, t3) → t4: niveles {t1}, {t2,t3}, {t4}."""
    t1 = _make_task(task_id="t1")
    t2 = _make_task(task_id="t2", depends_on=["t1"])
    t3 = _make_task(task_id="t3", depends_on=["t1"])
    t4 = _make_task(task_id="t4", depends_on=["t2", "t3"])
    levels = _topological_levels([t1, t2, t3, t4])
    assert [t.task_id for t in levels[0]] == ["t1"]
    assert {t.task_id for t in levels[1]} == {"t2", "t3"}
    assert [t.task_id for t in levels[2]] == ["t4"]


def test_topological_levels_breaks_cycle():
    """Ciclo → no entra en loop infinito; rompe con la primera task."""
    t1 = _make_task(task_id="t1", depends_on=["t2"])
    t2 = _make_task(task_id="t2", depends_on=["t1"])
    levels = _topological_levels([t1, t2])
    # Debe terminar sin colgar; un nivel contiene t1 y otro t2 (o viceversa).
    all_ids = {t.task_id for lv in levels for t in lv}
    assert all_ids == {"t1", "t2"}


# ── TeamPlanExecutor.execute ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_returns_responses_in_plan_order():
    """list[AgentResponse] viene en el mismo orden que plan.tasks, incluso
    si la ejecución fue en paralelo."""
    request = _make_request()
    t1 = _make_task(task_id="t1", agent="agent_income")
    t2 = _make_task(task_id="t2", agent="agent_stock")
    plan = AgentTeamPlan(plan_id="p1", intent="x", tasks=[t1, t2])

    income_agent = MagicMock()
    income_agent.process = AsyncMock(
        return_value=_make_response(request.request_id, agent_name="agent_income", summary="venta")
    )
    stock_agent = MagicMock()
    stock_agent.process = AsyncMock(
        return_value=_make_response(request.request_id, agent_name="agent_stock", summary="stock")
    )

    def fake_get(name, **_kwargs):
        return {"agent_income": income_agent, "agent_stock": stock_agent}[name]

    with (
        patch(f"{EXECUTOR_MOD}.get_sub_agent", side_effect=fake_get),
        _patch_session_factory(),
    ):
        executor = TeamPlanExecutor(redis=MagicMock(), user_id=uuid.uuid4(), tenant_id=uuid.uuid4())
        responses = await executor.execute(plan, request)

    assert [r.agent_name for r in responses] == ["agent_income", "agent_stock"]
    assert [r.result["summary"] for r in responses] == ["venta", "stock"]


@pytest.mark.asyncio
async def test_execute_parallel_level_uses_gather():
    """Tasks sin deps entre sí corren concurrentemente (asyncio.gather)."""
    request = _make_request()
    t1 = _make_task(task_id="t1", agent="agent_income")
    t2 = _make_task(task_id="t2", agent="agent_stock")
    plan = AgentTeamPlan(plan_id="p", intent="x", tasks=[t1, t2])

    events: list[str] = []

    async def slow_income(req, *, task):
        events.append("income_start")
        await asyncio.sleep(0.02)
        events.append("income_end")
        return _make_response(req.request_id, agent_name="agent_income")

    async def slow_stock(req, *, task):
        events.append("stock_start")
        await asyncio.sleep(0.02)
        events.append("stock_end")
        return _make_response(req.request_id, agent_name="agent_stock")

    income_agent = MagicMock()
    income_agent.process = AsyncMock(side_effect=slow_income)
    stock_agent = MagicMock()
    stock_agent.process = AsyncMock(side_effect=slow_stock)

    def fake_get(name, **_kwargs):
        return {"agent_income": income_agent, "agent_stock": stock_agent}[name]

    with (
        patch(f"{EXECUTOR_MOD}.get_sub_agent", side_effect=fake_get),
        _patch_session_factory(),
    ):
        executor = TeamPlanExecutor(redis=MagicMock(), user_id=uuid.uuid4(), tenant_id=uuid.uuid4())
        await executor.execute(plan, request)

    # Ambas tasks deben empezar antes de que alguna termine (paralelización real)
    assert events[0].endswith("_start")
    assert events[1].endswith("_start"), f"Esperaba paralelismo, got {events!r}"


@pytest.mark.asyncio
async def test_execute_sequential_respects_dependencies():
    """t2 depende de t1 → t1 termina antes de que t2 empiece."""
    request = _make_request()
    t1 = _make_task(task_id="t1", agent="agent_stock")
    t2 = _make_task(task_id="t2", agent="agent_expense", depends_on=["t1"])
    plan = AgentTeamPlan(plan_id="p", intent="x", tasks=[t1, t2])

    events: list[str] = []

    async def stock_run(req, *, task):
        events.append("stock_start")
        await asyncio.sleep(0.01)
        events.append("stock_end")
        return _make_response(req.request_id, agent_name="agent_stock")

    async def expense_run(req, *, task):
        events.append("expense_start")
        await asyncio.sleep(0.01)
        events.append("expense_end")
        return _make_response(req.request_id, agent_name="agent_expense")

    stock_agent = MagicMock(); stock_agent.process = AsyncMock(side_effect=stock_run)
    expense_agent = MagicMock(); expense_agent.process = AsyncMock(side_effect=expense_run)

    def fake_get(name, **_kw):
        return {"agent_stock": stock_agent, "agent_expense": expense_agent}[name]

    with (
        patch(f"{EXECUTOR_MOD}.get_sub_agent", side_effect=fake_get),
        _patch_session_factory(),
    ):
        executor = TeamPlanExecutor(redis=MagicMock(), user_id=uuid.uuid4(), tenant_id=uuid.uuid4())
        await executor.execute(plan, request)

    assert events == ["stock_start", "stock_end", "expense_start", "expense_end"]


@pytest.mark.asyncio
async def test_execute_skips_downstream_on_failure():
    """Si t1 falla, t2 (que depende de t1) se salta con upstream_failure."""
    request = _make_request()
    t1 = _make_task(task_id="t1", agent="agent_stock")
    t2 = _make_task(task_id="t2", agent="agent_expense", depends_on=["t1"])
    plan = AgentTeamPlan(plan_id="p", intent="x", tasks=[t1, t2])

    stock_agent = MagicMock()
    stock_agent.process = AsyncMock(
        return_value=_make_response(request.request_id, agent_name="agent_stock", status="error")
    )
    expense_agent = MagicMock()
    expense_agent.process = AsyncMock(
        return_value=_make_response(request.request_id, agent_name="agent_expense", status="success")
    )

    def fake_get(name, **_kw):
        return {"agent_stock": stock_agent, "agent_expense": expense_agent}[name]

    with (
        patch(f"{EXECUTOR_MOD}.get_sub_agent", side_effect=fake_get),
        _patch_session_factory(),
    ):
        executor = TeamPlanExecutor(redis=MagicMock(), user_id=uuid.uuid4(), tenant_id=uuid.uuid4())
        responses = await executor.execute(plan, request)

    # t1 falló → t2 nunca debe haberse llamado y queda en error con skipped_reason
    expense_agent.process.assert_not_called()
    assert responses[0].status == "error"
    assert responses[1].status == "error"
    assert responses[1].result["skipped_reason"] == "upstream_failure"
    assert responses[1].result["upstream_task_id"] == "t1"


@pytest.mark.asyncio
async def test_execute_parallel_failure_doesnt_skip_siblings():
    """Tasks paralelas (sin depender entre sí): si una falla, la otra igual corre."""
    request = _make_request()
    t1 = _make_task(task_id="t1", agent="agent_income")
    t2 = _make_task(task_id="t2", agent="agent_stock")
    plan = AgentTeamPlan(plan_id="p", intent="x", tasks=[t1, t2])

    income_agent = MagicMock()
    income_agent.process = AsyncMock(
        return_value=_make_response(request.request_id, agent_name="agent_income", status="error")
    )
    stock_agent = MagicMock()
    stock_agent.process = AsyncMock(
        return_value=_make_response(request.request_id, agent_name="agent_stock", status="success")
    )

    def fake_get(name, **_kw):
        return {"agent_income": income_agent, "agent_stock": stock_agent}[name]

    with (
        patch(f"{EXECUTOR_MOD}.get_sub_agent", side_effect=fake_get),
        _patch_session_factory(),
    ):
        executor = TeamPlanExecutor(redis=MagicMock(), user_id=uuid.uuid4(), tenant_id=uuid.uuid4())
        responses = await executor.execute(plan, request)

    # Las dos se ejecutaron (paralelas, sin dependencia)
    income_agent.process.assert_called_once()
    stock_agent.process.assert_called_once()
    assert responses[0].status == "error"
    assert responses[1].status == "success"


@pytest.mark.asyncio
async def test_execute_propagates_context_to_dependents():
    """El nivel N+1 recibe en request.context["upstream_outputs"] los results del nivel N."""
    request = _make_request()
    t1 = _make_task(task_id="t1", agent="agent_stock")
    t2 = _make_task(task_id="t2", agent="agent_expense", depends_on=["t1"])
    plan = AgentTeamPlan(plan_id="p", intent="x", tasks=[t1, t2])

    stock_response = _make_response(request.request_id, agent_name="agent_stock")
    stock_response.result["product_id"] = "abc123"

    captured_request: dict = {}

    async def expense_run(req, *, task):
        captured_request["request"] = req
        return _make_response(req.request_id, agent_name="agent_expense")

    stock_agent = MagicMock(); stock_agent.process = AsyncMock(return_value=stock_response)
    expense_agent = MagicMock(); expense_agent.process = AsyncMock(side_effect=expense_run)

    def fake_get(name, **_kw):
        return {"agent_stock": stock_agent, "agent_expense": expense_agent}[name]

    with (
        patch(f"{EXECUTOR_MOD}.get_sub_agent", side_effect=fake_get),
        _patch_session_factory(),
    ):
        executor = TeamPlanExecutor(redis=MagicMock(), user_id=uuid.uuid4(), tenant_id=uuid.uuid4())
        await executor.execute(plan, request)

    downstream_req: AgentRequest = captured_request["request"]
    upstream = downstream_req.context.get("upstream_outputs", {})
    assert "t1" in upstream
    assert upstream["t1"]["product_id"] == "abc123"


@pytest.mark.asyncio
async def test_execute_uses_isolated_session_per_task():
    """Cada task obtiene una sesión nueva via async_session_factory."""
    request = _make_request()
    t1 = _make_task(task_id="t1", agent="agent_income")
    t2 = _make_task(task_id="t2", agent="agent_stock")
    plan = AgentTeamPlan(plan_id="p", intent="x", tasks=[t1, t2])

    sessions_handed_out: list[MagicMock] = []

    def make_cm():
        session = MagicMock()
        sessions_handed_out.append(session)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=None)
        return cm

    income_agent = MagicMock()
    income_agent.process = AsyncMock(return_value=_make_response(request.request_id, agent_name="agent_income"))
    stock_agent = MagicMock()
    stock_agent.process = AsyncMock(return_value=_make_response(request.request_id, agent_name="agent_stock"))

    def fake_get(name, **_kw):
        return {"agent_income": income_agent, "agent_stock": stock_agent}[name]

    with (
        patch(f"{EXECUTOR_MOD}.get_sub_agent", side_effect=fake_get),
        patch(f"{EXECUTOR_MOD}.async_session_factory", side_effect=make_cm),
    ):
        executor = TeamPlanExecutor(redis=MagicMock(), user_id=uuid.uuid4(), tenant_id=uuid.uuid4())
        await executor.execute(plan, request)

    # Dos tasks → dos sesiones distintas
    assert len(sessions_handed_out) == 2
    assert sessions_handed_out[0] is not sessions_handed_out[1]


@pytest.mark.asyncio
async def test_execute_agent_exception_in_parallel_marks_failed():
    """Exception en una task paralela queda capturada como AgentResponse(status=error)."""
    request = _make_request()
    t1 = _make_task(task_id="t1", agent="agent_income")
    t2 = _make_task(task_id="t2", agent="agent_stock")
    plan = AgentTeamPlan(plan_id="p", intent="x", tasks=[t1, t2])

    income_agent = MagicMock()
    income_agent.process = AsyncMock(side_effect=RuntimeError("boom"))
    stock_agent = MagicMock()
    stock_agent.process = AsyncMock(
        return_value=_make_response(request.request_id, agent_name="agent_stock")
    )

    def fake_get(name, **_kw):
        return {"agent_income": income_agent, "agent_stock": stock_agent}[name]

    with (
        patch(f"{EXECUTOR_MOD}.get_sub_agent", side_effect=fake_get),
        _patch_session_factory(),
    ):
        executor = TeamPlanExecutor(redis=MagicMock(), user_id=uuid.uuid4(), tenant_id=uuid.uuid4())
        responses = await executor.execute(plan, request)

    assert responses[0].status == "error"
    assert responses[1].status == "success"


@pytest.mark.asyncio
async def test_execute_unknown_agent_returns_error_response():
    """get_sub_agent retornando None → AgentResponse error, no rompe el plan."""
    request = _make_request()
    t1 = _make_task(task_id="t1", agent="agent_unknown")
    plan = AgentTeamPlan(plan_id="p", intent="x", tasks=[t1])

    with (
        patch(f"{EXECUTOR_MOD}.get_sub_agent", return_value=None),
        _patch_session_factory(),
    ):
        executor = TeamPlanExecutor(redis=MagicMock(), user_id=uuid.uuid4(), tenant_id=uuid.uuid4())
        responses = await executor.execute(plan, request)

    assert len(responses) == 1
    assert responses[0].status == "error"
    assert "no disponible" in (responses[0].message or "").lower()
