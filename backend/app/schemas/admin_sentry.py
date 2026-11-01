"""Schemas de `/admin/sentry/issues` (panel SUPERADMIN)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class SentryIssue(BaseModel):
    id: str
    #: Identificador legible, p. ej. `PYTHON-FASTAPI-B`.
    short_id: str
    title: str
    project: str
    #: Tag `service` (api / worker / mcp) cuando el evento lo trae.
    service: str | None
    level: str | None
    #: Cantidad de eventos del issue (Sentry la manda como string).
    events: int
    first_seen: datetime | None
    last_seen: datetime | None
    #: Enlace al issue en Sentry.
    url: str


class SentryIssuesResponse(BaseModel):
    #: `False` = falta `SENTRY_AUTH_TOKEN` o `SENTRY_ORG`: no es un error, y
    #: `issues` viene vacío sin que eso signifique "cero errores".
    configured: bool
    dias: int
    projects: list[str]
    issues: list[SentryIssue]
    #: Cuántos había antes de recortar la lista.
    total: int
