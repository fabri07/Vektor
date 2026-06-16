"""Correlación de trazas: un `trace_id` por request, accesible desde código de app.

structlog ya tiene su propio contextvar para los logs; este `ContextVar` estándar
permite que el código de aplicación (audit logs, external_operation_logs) lea el
trace_id vigente en tiempo de INSERT — p. ej. como `default=` de una columna ORM,
sin tener que pasarlo por la firma de cada función.

El middleware HTTP lo setea al inicio del request (`set_trace_id`). Fuera de un
request (jobs Celery), `get_trace_id()` devuelve None.
"""

from contextvars import ContextVar

_trace_id_var: ContextVar[str | None] = ContextVar("trace_id", default=None)


def set_trace_id(trace_id: str | None) -> None:
    _trace_id_var.set(trace_id)


def get_trace_id() -> str | None:
    return _trace_id_var.get()
