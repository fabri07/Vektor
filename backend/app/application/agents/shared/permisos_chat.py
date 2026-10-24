"""Quién puede escribir datos del negocio desde el chat (R-1).

El chat ejecuta acciones por dos caminos: la auto-ejecución (`REGISTER_EXPENSE`,
las reglas automáticas) y la aprobación de una acción pendiente
(`/agent/confirm`, «sí» por texto). La aprobación ya exigía OWNER/ADMIN; la
auto-ejecución no miraba el rol, y un VIEWER que escribía «gasté 5000 en luz»
dejaba el gasto persistido (revisión profunda 2026-10-24).

Para un rol que no confirma, una respuesta que escribiría algo se reemplaza por
un rechazo claro: no se ejecuta ni queda pendiente. Una pendiente de un VIEWER
no la aprobaría nadie (vence a los 10 minutos y el dueño no tiene dónde verla),
así que prometerla era peor que decir que no. La capa HTTP lo garantiza
(`api/v1/agent.py::_process_agent_action`); el orquestador lo aplica antes de
narrar, para no gastar el LLM y para que la memoria de la conversación diga lo
que pasó.
"""

from __future__ import annotations

from typing import Any

from app.application.agents.shared.schemas import ActionType, AgentResponse

#: Los roles que confirman acciones (los mismos que `/agent/confirm`).
ROLES_QUE_EJECUTAN: frozenset[str] = frozenset({"OWNER", "ADMIN"})

#: Las acciones que el chat ejecuta sin pedir aprobación.
ACCIONES_AUTO_EJECUTABLES: frozenset[str] = frozenset({str(ActionType.REGISTER_EXPENSE)})

MENSAJE_SIN_PERMISO = (
    "Tu usuario es de solo lectura: desde el chat no puede registrar ni modificar "
    "datos del negocio. Pedile al dueño o a un administrador que lo cargue."
)


def _escribe(requires_approval: bool, result: dict[str, Any] | None) -> bool:
    datos = result or {}
    return bool(
        requires_approval
        or datos.get("auto_execute")
        or str(datos.get("action_type") or "") in ACCIONES_AUTO_EJECUTABLES
    )


def quiere_escribir(respuesta: AgentResponse) -> bool:
    """¿La respuesta ejecutaría algo o dejaría algo para aprobar?"""
    if _escribe(respuesta.requires_approval, respuesta.result):
        return True
    tareas = (respuesta.result or {}).get("task_responses") or []
    return any(
        isinstance(t, dict) and _escribe(bool(t.get("requires_approval")), t) for t in tareas
    )


def rechazar_por_rol(respuesta: AgentResponse) -> None:
    """Convierte la respuesta en el rechazo: nada se ejecuta ni queda pendiente."""
    respuesta.requires_approval = False
    respuesta.status = "success"
    respuesta.pending_action_id = None
    resultado = respuesta.result if respuesta.result is not None else {}
    resultado.pop("auto_execute", None)
    for tarea in resultado.get("task_responses") or []:
        if isinstance(tarea, dict):
            tarea["requires_approval"] = False
            tarea.pop("auto_execute", None)
            tarea["pending_action_id"] = None
    resultado["blocked_by_role"] = True
    resultado["summary"] = MENSAJE_SIN_PERMISO
    respuesta.result = resultado
    respuesta.message = MENSAJE_SIN_PERMISO
