"""FASE 5: diagnóstico de la integración Google MCP.

El código de la integración MCP está completo; los fallos al conectar Google son
operacionales (flag, URL, shared secret, conectividad, redirect URI en Google
Cloud, scopes). Este servicio chequea todo lo verificable desde el backend y
reporta exactamente qué falta, sin exponer secretos y sin romper nunca (cada
check captura su error). Lo que NO se puede verificar desde acá (redirect URI en
Google Cloud Console, app verification) se reporta como hint informativo.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx

from app.config.settings import get_settings
from app.observability.logger import get_logger
from app.persistence.models.google_mcp_connection import GoogleMcpConnection

logger = get_logger(__name__)

# Scopes que el backend solicita (deben estar registrados/concedidos en Google Cloud).
_EXPECTED_SCOPES = [
    "gmail.readonly",
    "gmail.compose",
    "gmail.send",
    "calendar.events",
    "spreadsheets",
    "documents",
    "drive.readonly",
    "drive.file",
]


def _check(name: str, ok: bool, severity: str, detail: str) -> dict[str, Any]:
    return {"check": name, "ok": ok, "severity": severity, "detail": detail}


async def run_google_mcp_diagnostics(
    session: Any,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
) -> dict[str, Any]:
    """Corre los chequeos de configuración/conectividad de Google MCP.

    Devuelve {overall_ok, checks[], tenant_connection}. `overall_ok` es False si
    algún check de severidad 'error' falla. Nunca lanza: cada paso es fail-safe.
    """
    settings = get_settings()
    checks: list[dict[str, Any]] = []

    flag_on = bool(settings.ENABLE_GOOGLE_MCP_TOOLS)
    checks.append(
        _check(
            "flag_enabled",
            flag_on,
            "error",
            "ENABLE_GOOGLE_MCP_TOOLS activo."
            if flag_on
            else "ENABLE_GOOGLE_MCP_TOOLS está apagado: el backend ignora el MCP. Activalo.",
        )
    )

    mcp_url = (settings.MCP_SERVER_URL or "").rstrip("/")
    url_ok = bool(mcp_url)
    checks.append(
        _check(
            "mcp_url_configured",
            url_ok,
            "error",
            f"MCP_SERVER_URL = {mcp_url}" if url_ok else "MCP_SERVER_URL vacío.",
        )
    )

    secret_ok = bool(getattr(settings, "MCP_SERVER_SHARED_SECRET", ""))
    checks.append(
        _check(
            "shared_secret_configured",
            secret_ok,
            "warning",
            "MCP_SERVER_SHARED_SECRET presente (no se expone)."
            if secret_ok
            else "MCP_SERVER_SHARED_SECRET vacío: el MCP server rechaza las llamadas con 401.",
        )
    )

    # Conectividad al MCP server (no requiere secret): GET /health.
    if url_ok:
        reachable, detail = await _check_reachable(mcp_url)
        checks.append(_check("mcp_server_reachable", reachable, "error", detail))

        # Auth/estado: valida secret + reporta el estado del tenant en el MCP server.
        auth_ok, auth_detail = await _check_auth(settings, tenant_id, user_id)
        checks.append(_check("mcp_auth", auth_ok, "error", auth_detail))

    # Redirect URI / Google Cloud: no verificable desde el backend → hint.
    checks.append(
        _check(
            "redirect_uri_hint",
            True,
            "info",
            "Verificá en Google Cloud Console que el Redirect URI registrado sea el del "
            "MCP server (.../auth/callback), NO el del backend, y que coincida exactamente "
            "con GOOGLE_MCP_OAUTH_REDIRECT_URI del MCP server.",
        )
    )
    checks.append(
        _check(
            "scopes_hint",
            True,
            "info",
            "Scopes solicitados: " + ", ".join(_EXPECTED_SCOPES) + ". Deben estar habilitados "
            "en la pantalla de consentimiento de Google Cloud y la app verificada (o el "
            "usuario en la lista de testers) para evitar el warning 'app no verificada'.",
        )
    )

    tenant_connection = await _tenant_connection(session, tenant_id, user_id)

    overall_ok = all(c["ok"] for c in checks if c["severity"] == "error")
    return {"overall_ok": overall_ok, "checks": checks, "tenant_connection": tenant_connection}


async def _check_reachable(mcp_url: str) -> tuple[bool, str]:
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{mcp_url}/health", timeout=5.0)
        if resp.status_code == 200:
            return True, "MCP server responde /health (200)."
        return False, f"MCP server respondió /health con HTTP {resp.status_code}."
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        return False, f"MCP server inalcanzable en {mcp_url}: {exc}."
    except Exception as exc:  # noqa: BLE001 — diagnóstico nunca rompe
        return False, f"Error al contactar el MCP server: {exc}."


async def _check_auth(
    settings: Any, tenant_id: uuid.UUID, user_id: uuid.UUID
) -> tuple[bool, str]:
    try:
        from app.integrations.mcp.http_gateway import HttpMcpGateway  # noqa: PLC0415

        gateway = HttpMcpGateway(settings=settings)
        status = await gateway.get_auth_status(
            tenant_id=str(tenant_id), user_id=str(user_id)
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"No se pudo consultar /auth/status: {exc}."

    last_error = status.get("last_error")
    if last_error == "http_401":
        return False, "El MCP server devolvió 401: MCP_SERVER_SHARED_SECRET no coincide."
    if last_error == "mcp_unavailable":
        return False, "MCP server inalcanzable al consultar /auth/status."
    if last_error and last_error.startswith("http_"):
        return False, f"El MCP server devolvió {last_error} en /auth/status."
    # 200: conectividad + secret OK (aunque el tenant no esté conectado todavía).
    return True, "Conectividad + shared secret OK (/auth/status respondió)."


async def _tenant_connection(
    session: Any, tenant_id: uuid.UUID, user_id: uuid.UUID
) -> dict[str, Any] | None:
    try:
        from sqlalchemy import select  # noqa: PLC0415

        result = await session.execute(
            select(GoogleMcpConnection).where(
                GoogleMcpConnection.tenant_id == tenant_id,
                GoogleMcpConnection.user_id == user_id,
            )
        )
        conn = result.scalar_one_or_none()
        if conn is None:
            return None
        return {
            "status": conn.status,
            "last_error_code": conn.last_error_code,
            "scopes_granted": conn.scopes_granted or [],
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("mcp_diagnostics.tenant_connection_failed", error=str(exc))
        return None
