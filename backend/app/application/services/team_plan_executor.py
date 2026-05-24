"""TeamPlanExecutor — orquestación de planes multi-task con paralelismo y DAG.

Flujo:
  1. Calcular niveles topológicos a partir de depends_on
  2. Por nivel: asyncio.gather para tasks paralelas, o ejecución directa si es única
  3. Cada task obtiene su propia AsyncSession (no concurrent-safe compartir sesiones)
  4. Retorna list[AgentResponse] en el orden original del plan
"""

from __future__ import annotations

import asyncio
import uuid

from redis.asyncio import Redis

from app.application.agents.registry import get_sub_agent
from app.application.agents.shared.schemas import (
    AgentRequest,
    AgentResponse,
    AgentTask,
    AgentTeamPlan,
)
from app.observability.logger import get_logger
from app.persistence.db.session import async_session_factory

logger = get_logger(__name__)


def _topological_levels(tasks: list[AgentTask]) -> list[list[AgentTask]]:
    """Ordena tasks por dependencias. Tasks en el mismo nivel corren en paralelo."""
    remaining = list(tasks)
    completed_ids: set[str] = set()
    levels: list[list[AgentTask]] = []

    while remaining:
        ready = [
            t for t in remaining
            if all(dep in completed_ids for dep in t.depends_on)
        ]
        if not ready:
            # Dependencia circular o inválida — romper el ciclo con la primera task restante
            logger.error(
                "team_plan_executor_circular_dependency",
                remaining_ids=[t.task_id for t in remaining],
            )
            ready = [remaining[0]]

        levels.append(ready)
        ready_ids = {t.task_id for t in ready}
        completed_ids |= ready_ids
        remaining = [t for t in remaining if t.task_id not in ready_ids]

    return levels


class TeamPlanExecutor:
    def __init__(
        self,
        redis: Redis,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
    ) -> None:
        self._redis = redis
        self._user_id = user_id
        self._tenant_id = tenant_id

    async def execute(
        self,
        plan: AgentTeamPlan,
        request: AgentRequest,
    ) -> list[AgentResponse]:
        """Ejecuta el plan respetando dependencias. Tareas sin dependencias pendientes
        corren en paralelo con asyncio.gather y sesiones DB aisladas."""
        levels = _topological_levels(plan.tasks)
        responses_by_id: dict[str, AgentResponse] = {}

        for level_tasks in levels:
            if len(level_tasks) == 1:
                task = level_tasks[0]
                resp = await self._run_task(task, request)
                responses_by_id[task.task_id] = resp
            else:
                results = await asyncio.gather(
                    *[self._run_task(t, request) for t in level_tasks],
                    return_exceptions=True,
                )
                for task, result in zip(level_tasks, results, strict=True):
                    if isinstance(result, BaseException):
                        logger.error(
                            "team_plan_executor_task_exception",
                            task_id=task.task_id,
                            agent=task.agent,
                            error=str(result),
                            error_type=type(result).__name__,
                        )
                        responses_by_id[task.task_id] = AgentResponse(
                            request_id=request.request_id,
                            agent_name=task.agent,
                            status="error",
                            risk_level="LOW",
                            confidence="LOW",
                            message="Error interno al ejecutar la tarea.",
                            result={"error": str(result), "task_id": task.task_id},
                        )
                    else:
                        responses_by_id[task.task_id] = result  # type: ignore[assignment]

        return [responses_by_id[t.task_id] for t in plan.tasks]

    async def _run_task(self, task: AgentTask, request: AgentRequest) -> AgentResponse:
        """Ejecuta una tarea con sesión DB propia — aislada del resto del plan."""
        async with async_session_factory() as own_db:
            agent = get_sub_agent(
                task.agent,
                db=own_db,
                redis=self._redis,
                user_id=self._user_id,
                tenant_id=self._tenant_id,
            )
            if agent is None:
                logger.error(
                    "team_plan_executor_agent_not_found",
                    agent=task.agent,
                    task_id=task.task_id,
                )
                return AgentResponse(
                    request_id=request.request_id,
                    agent_name=task.agent,
                    status="error",
                    risk_level="LOW",
                    confidence="LOW",
                    message=f"Agente '{task.agent}' no disponible.",
                    result={"task_id": task.task_id},
                )
            try:
                return await agent.process(request, task=task)
            except Exception as exc:
                logger.error(
                    "team_plan_executor_agent_failed",
                    agent=task.agent,
                    task_id=task.task_id,
                    error=str(exc),
                    error_type=type(exc).__name__,
                    exc_info=True,
                )
                return AgentResponse(
                    request_id=request.request_id,
                    agent_name=task.agent,
                    status="error",
                    risk_level="LOW",
                    confidence="LOW",
                    message="Error al ejecutar la tarea.",
                    result={"error": str(exc), "task_id": task.task_id},
                )
