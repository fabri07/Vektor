"""Lectura de issues de Sentry para el panel SUPERADMIN.

Sólo LEE (GET) con un token de sólo lectura. La organización de Sentry puede
compartirse con otras apps del dueño, y un token de organización ve TODOS sus
proyectos: por eso el servicio filtra por la lista cerrada `SENTRY_PROJECTS` y
nunca muestra un issue de otro proyecto, aunque la API lo devuelva.

El token no se loguea ni viaja en ninguna respuesta. Un fallo de Sentry se
traduce a `SentryUnavailableError` con un código, sin el cuerpo remoto.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Final

import httpx
from redis.asyncio import Redis

from app.config.settings import get_settings
from app.observability.logger import get_logger
from app.schemas.admin_sentry import SentryIssue, SentryIssuesResponse

logger = get_logger(__name__)

VENTANA_DIAS: Final[int] = 14
MAX_ISSUES: Final[int] = 25
CACHE_SEGUNDOS: Final[int] = 60
TIMEOUT_SEGUNDOS: Final[float] = 10.0
_CACHE_KEY: Final[str] = "admin:sentry:issues:v1"


class SentryUnavailableError(Exception):
    """Sentry no respondió bien. `code` es estable; el detalle remoto no se expone."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _proyectos_permitidos() -> list[str]:
    crudo = get_settings().SENTRY_PROJECTS
    return [p.strip() for p in crudo.split(",") if p.strip()]


def _cliente() -> httpx.AsyncClient:
    """Punto único de construcción del cliente HTTP (los tests lo reemplazan)."""
    s = get_settings()
    return httpx.AsyncClient(
        base_url=s.SENTRY_API_BASE.rstrip("/"),
        headers={"Authorization": f"Bearer {s.SENTRY_AUTH_TOKEN}"},
        timeout=TIMEOUT_SEGUNDOS,
    )


def _fecha(valor: Any) -> datetime | None:
    if not isinstance(valor, str):
        return None
    try:
        return datetime.fromisoformat(valor.replace("Z", "+00:00"))
    except ValueError:
        return None


def _entero(valor: Any) -> int:
    try:
        return int(valor)
    except (TypeError, ValueError):
        return 0


def _a_issue(crudo: dict[str, Any], permitidos: set[str]) -> SentryIssue | None:
    proyecto = crudo.get("project")
    slug = proyecto.get("slug") if isinstance(proyecto, dict) else None
    if not isinstance(slug, str) or slug not in permitidos:
        return None
    service = next(
        (
            t.get("value")
            for t in crudo.get("tags", [])
            if isinstance(t, dict) and t.get("key") == "service"
        ),
        None,
    )
    return SentryIssue(
        id=str(crudo.get("id", "")),
        short_id=str(crudo.get("shortId", "")),
        title=str(crudo.get("title", "")),
        project=slug,
        service=service if isinstance(service, str) else None,
        level=crudo.get("level") if isinstance(crudo.get("level"), str) else None,
        events=_entero(crudo.get("count")),
        first_seen=_fecha(crudo.get("firstSeen")),
        last_seen=_fecha(crudo.get("lastSeen")),
        url=str(crudo.get("permalink", "")),
    )


async def _traer_proyecto(
    http: httpx.AsyncClient, org: str, proyecto: str
) -> list[dict[str, Any]]:
    """Issues sin resolver de UN proyecto (ruta por slug: no depende de IDs numéricos)."""
    r = await http.get(
        f"/api/0/projects/{org}/{proyecto}/issues/",
        params={"query": "is:unresolved", "statsPeriod": f"{VENTANA_DIAS}d", "limit": "100"},
    )
    if r.status_code in (401, 403):
        logger.warning("sentry_issues.rejected", status=r.status_code, project=proyecto)
        raise SentryUnavailableError("SENTRY_TOKEN_REJECTED")
    if r.status_code != 200:
        logger.warning("sentry_issues.bad_status", status=r.status_code, project=proyecto)
        raise SentryUnavailableError("SENTRY_BAD_RESPONSE")
    try:
        datos = r.json()
    except ValueError as exc:
        raise SentryUnavailableError("SENTRY_BAD_RESPONSE") from exc
    if not isinstance(datos, list):
        raise SentryUnavailableError("SENTRY_BAD_RESPONSE")
    return [d for d in datos if isinstance(d, dict)]


async def _traer(org: str, proyectos: list[str]) -> list[dict[str, Any]]:
    crudos: list[dict[str, Any]] = []
    try:
        async with _cliente() as http:
            for proyecto in proyectos:
                crudos.extend(await _traer_proyecto(http, org, proyecto))
    except httpx.HTTPError as exc:
        logger.warning("sentry_issues.unreachable", error_type=type(exc).__name__)
        raise SentryUnavailableError("SENTRY_UNREACHABLE") from exc
    return crudos


async def get_issues(redis: Redis) -> SentryIssuesResponse:
    s = get_settings()
    proyectos = _proyectos_permitidos()
    if not s.SENTRY_AUTH_TOKEN or not s.SENTRY_ORG or not proyectos:
        return SentryIssuesResponse(
            configured=False, dias=VENTANA_DIAS, projects=proyectos, issues=[], total=0
        )

    try:
        en_cache = await redis.get(_CACHE_KEY)
        if en_cache:
            return SentryIssuesResponse.model_validate_json(en_cache)
    except Exception as exc:  # noqa: BLE001 - el cache nunca rompe la consulta
        logger.warning("sentry_issues.cache_read_failed", error_type=type(exc).__name__)

    permitidos = set(proyectos)
    issues = [
        i
        for i in (_a_issue(c, permitidos) for c in await _traer(s.SENTRY_ORG, proyectos))
        if i is not None
    ]
    issues.sort(key=lambda i: i.last_seen.timestamp() if i.last_seen else 0.0, reverse=True)
    respuesta = SentryIssuesResponse(
        configured=True,
        dias=VENTANA_DIAS,
        projects=proyectos,
        issues=issues[:MAX_ISSUES],
        total=len(issues),
    )
    try:
        serializado = json.dumps(respuesta.model_dump(mode="json"))
        await redis.set(_CACHE_KEY, serializado, ex=CACHE_SEGUNDOS)
    except Exception as exc:  # noqa: BLE001
        logger.warning("sentry_issues.cache_write_failed", error_type=type(exc).__name__)
    return respuesta
