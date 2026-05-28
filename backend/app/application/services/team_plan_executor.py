"""TeamPlanExecutor — orquestación de planes multi-task con paralelismo y DAG.

Flujo:
  1. Calcular niveles topológicos a partir de depends_on
  2. Por nivel: asyncio.gather para tasks paralelas, o ejecución directa si es única
  3. Cada task obtiene su propia AsyncSession (no concurrent-safe compartir sesiones)
  4. Outputs de nivel N se inyectan en request.context["upstream_outputs"] para nivel N+1
  5. Si una task falla, las que dependen de ella se saltan con status="error"
     y result["skipped_reason"] = "upstream_failure"
  6. Retorna list[AgentResponse] en el orden original del plan
"""

from __future__ import annotations

import asyncio
import uuid

from redis.asyncio import Redis

from app.application.agents.registry import get_sub_agent
from app.application.agents.shared.schemas import (
    ActionType,
    AgentRequest,
    AgentResponse,
    AgentTask,
    AgentTeamPlan,
)
from app.observability.logger import get_logger
from app.persistence.db.session import async_session_factory

logger = get_logger(__name__)

# Tipos de acción que involucran MCP externo: merecen un retry ante fallos transientes
_EXTERNAL_ACTION_TYPES: frozenset[str] = frozenset({
    str(ActionType.SYNC_TO_GOOGLE),
    str(ActionType.CREATE_CALENDAR_EVENT),
    str(ActionType.CREATE_SUPPLIER_DRAFT),
    str(ActionType.CLASSIFY_GMAIL_MESSAGE),
    str(ActionType.UPLOAD_TO_DRIVE),
    str(ActionType.CREATE_GOOGLE_DOC),
    str(ActionType.APPEND_TO_SHEET),
})

_RETRY_BACKOFF_SECONDS = 2.0  # espera entre intento 1 y retry 1


class InvalidPlanDAGError(ValueError):
    """El plan tiene un ciclo o referencia a un task_id inexistente."""

    def __init__(self, message: str, *, remaining_ids: list[str]):
        super().__init__(message)
        self.remaining_ids = remaining_ids


def _topological_levels(tasks: list[AgentTask]) -> list[list[AgentTask]]:
    """Ordena tasks por dependencias. Tasks en el mismo nivel corren en paralelo.

    Lanza InvalidPlanDAGError si el plan contiene un ciclo o referencias a
    task_ids que no existen en el plan. La validación es estricta: un plan
    inválido nunca se ejecuta parcialmente.
    """
    known_ids = {t.task_id for t in tasks}
    for t in tasks:
        invalid = [d for d in t.depends_on if d not in known_ids]
        if invalid:
            raise InvalidPlanDAGError(
                f"Task {t.task_id} depende de id(s) inexistente(s): {invalid}",
                remaining_ids=[t.task_id],
            )

    remaining = list(tasks)
    completed_ids: set[str] = set()
    levels: list[list[AgentTask]] = []

    while remaining:
        ready = [
            t for t in remaining
            if all(dep in completed_ids for dep in t.depends_on)
        ]
        if not ready:
            # Ciclo: no hay tasks listas pero quedan pendientes
            logger.error(
                "team_plan_executor_circular_dependency",
                remaining_ids=[t.task_id for t in remaining],
            )
            raise InvalidPlanDAGError(
                "Dependencia circular detectada en el plan.",
                remaining_ids=[t.task_id for t in remaining],
            )

        levels.append(ready)
        ready_ids = {t.task_id for t in ready}
        completed_ids |= ready_ids
        remaining = [t for t in remaining if t.task_id not in ready_ids]

    return levels


def _is_failure(resp: AgentResponse) -> bool:
    """Determina si una AgentResponse cuenta como fallo upstream para sus dependientes."""
    return resp.status == "error"


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
        corren en paralelo con asyncio.gather y sesiones DB aisladas.

        Outputs de nivel N se pasan en `request.context["upstream_outputs"]` a nivel N+1.
        Si una task falla, las dependientes se saltan automáticamente.

        Si el plan tiene un DAG inválido (ciclo o depends_on huérfano), retorna
        una list[AgentResponse] con status="error" para cada task — el plan no se
        ejecuta parcialmente.
        """
        try:
            levels = _topological_levels(plan.tasks)
        except InvalidPlanDAGError as exc:
            logger.error(
                "team_plan_executor_invalid_dag",
                plan_id=plan.plan_id,
                remaining_ids=exc.remaining_ids,
                error=str(exc),
            )
            return [
                AgentResponse(
                    request_id=request.request_id,
                    agent_name=t.agent,
                    status="error",
                    risk_level="LOW",
                    confidence="LOW",
                    message="Plan inválido: no se ejecutó.",
                    result={
                        "task_id": t.task_id,
                        "skipped_reason": "invalid_plan_dag",
                        "error": str(exc),
                    },
                )
                for t in plan.tasks
            ]
        responses_by_id: dict[str, AgentResponse] = {}
        failed_ids: set[str] = set()

        for level_tasks in levels:
            # Particionar el nivel: skipped (dep upstream falló) vs ejecutables
            to_skip: list[AgentTask] = []
            to_run: list[AgentTask] = []
            for task in level_tasks:
                failed_upstream = [d for d in task.depends_on if d in failed_ids]
                if failed_upstream:
                    to_skip.append(task)
                else:
                    to_run.append(task)

            for task in to_skip:
                upstream = next(d for d in task.depends_on if d in failed_ids)
                logger.warning(
                    "team_plan_executor_skip_downstream",
                    task_id=task.task_id,
                    agent=task.agent,
                    upstream_task_id=upstream,
                )
                skipped = AgentResponse(
                    request_id=request.request_id,
                    agent_name=task.agent,
                    status="error",
                    risk_level="LOW",
                    confidence="LOW",
                    message="Tarea omitida: dependencia falló.",
                    result={
                        "task_id": task.task_id,
                        "skipped_reason": "upstream_failure",
                        "upstream_task_id": upstream,
                    },
                )
                responses_by_id[task.task_id] = skipped
                failed_ids.add(task.task_id)

            if not to_run:
                continue

            if len(to_run) == 1:
                task = to_run[0]
                task_request = self._request_for_task(request, task, responses_by_id)
                resp = await self._run_task(task, task_request)
                responses_by_id[task.task_id] = resp
                if _is_failure(resp):
                    failed_ids.add(task.task_id)
            else:
                task_requests = [
                    self._request_for_task(request, t, responses_by_id) for t in to_run
                ]
                results = await asyncio.gather(
                    *[self._run_task(t, r) for t, r in zip(to_run, task_requests, strict=True)],
                    return_exceptions=True,
                )
                for task, result in zip(to_run, results, strict=True):
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
                        failed_ids.add(task.task_id)
                    else:
                        responses_by_id[task.task_id] = result  # type: ignore[assignment]
                        if _is_failure(result):  # type: ignore[arg-type]
                            failed_ids.add(task.task_id)

        return [responses_by_id[t.task_id] for t in plan.tasks]

    def _request_for_task(
        self,
        base_request: AgentRequest,
        task: AgentTask,
        responses_by_id: dict[str, AgentResponse],
    ) -> AgentRequest:
        """Clona el request inyectando outputs de tareas upstream en context."""
        if not task.depends_on:
            return base_request
        upstream_outputs = {
            dep_id: responses_by_id[dep_id].result
            for dep_id in task.depends_on
            if dep_id in responses_by_id
        }
        if not upstream_outputs:
            return base_request
        new_context = dict(base_request.context)
        existing = dict(new_context.get("upstream_outputs", {}))
        existing.update(upstream_outputs)
        new_context["upstream_outputs"] = existing
        return base_request.model_copy(update={"context": new_context})

    async def _run_task(self, task: AgentTask, request: AgentRequest) -> AgentResponse:
        """Ejecuta una tarea con sesión DB propia.

        Para tasks externas (MCP Google): 1 retry con backoff de _RETRY_BACKOFF_SECONDS
        si el primer intento lanza una excepción (transient failure).
        No reintenta si el agente retorna requires_google_auth — eso requiere intervención humana.
        """
        is_external = str(task.action_type) in _EXTERNAL_ACTION_TYPES
        attempts = 2 if is_external else 1

        for attempt in range(1, attempts + 1):
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
                    resp = await agent.process(request, task=task)
                    if attempt > 1:
                        logger.info(
                            "team_plan_executor_retry_succeeded",
                            agent=task.agent,
                            task_id=task.task_id,
                            attempt=attempt,
                        )
                    return resp
                except Exception as exc:
                    if attempt < attempts:
                        logger.warning(
                            "team_plan_executor_agent_failed_retrying",
                            agent=task.agent,
                            task_id=task.task_id,
                            attempt=attempt,
                            error=str(exc),
                            error_type=type(exc).__name__,
                        )
                        await asyncio.sleep(_RETRY_BACKOFF_SECONDS)
                        continue
                    logger.error(
                        "team_plan_executor_agent_failed",
                        agent=task.agent,
                        task_id=task.task_id,
                        attempt=attempt,
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
        # unreachable — satisface type checker
        raise RuntimeError("_run_task loop exited without return")
