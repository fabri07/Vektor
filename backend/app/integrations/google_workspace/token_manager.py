"""TokenManager — gestión del ciclo de vida de tokens OAuth para Workspace.

Responsabilidades:
  1. Cargar la conexión activa del usuario desde la DB.
  2. Descifrar el access_token y chequear expiración (buffer 5 min).
  3. Si está por vencer: refresh con SELECT ... FOR UPDATE + double-check post-lock
     (evita que dos requests concurrentes llamen a Google simultáneamente).
  4. Re-cifrar el nuevo token y persistir en DB (flush, no commit).
  5. Propagación de errores con WorkspaceTokenError.

Invariantes:
  - El refresh_token NUNCA se loguea.
  - Si el refresh falla, se actualiza last_error_code/last_error_at y se lanza error.
  - SELECT FOR UPDATE garantiza que solo un worker refresca a la vez por usuario.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.security.token_cipher import TokenCipherError, decrypt_token, encrypt_token
from app.config.settings import get_settings
from app.integrations.google_workspace.exceptions import WorkspaceTokenError
from app.observability.logger import get_logger
from app.persistence.models.user_google_workspace import UserGoogleWorkspaceConnection

logger = get_logger(__name__)
settings = get_settings()

_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_REFRESH_BUFFER = timedelta(minutes=5)


class TokenManager:
    """Gestiona obtención y renovación del access_token de Workspace.

    Args:
        session:   AsyncSession abierta (sin commit — el caller lo hace).
        user_id:   UUID del usuario autenticado.
        tenant_id: UUID del tenant (enforced en todas las queries).
        http_client: httpx.AsyncClient inyectado en tests; None crea uno nuevo en producción.
    """

    def __init__(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._session = session
        self._user_id = user_id
        self._tenant_id = tenant_id
        self._http = http_client

    # ── API pública ───────────────────────────────────────────────────────────

    async def get_valid_access_token(self) -> str:
        """Retorna un access_token vigente (descifrado, texto plano).

        Refresca automáticamente si el token vence en menos de 5 minutos.

        Raises:
            WorkspaceTokenError(reason="not_connected")      — sin conexión activa
            WorkspaceTokenError(reason="refresh_failed")     — Google rechazó el refresh
            WorkspaceTokenError(reason="token_corrupted")    — descifrado Fernet fallido
        """
        conn = await self._load_connection()
        if conn is None or not conn.is_active:
            raise WorkspaceTokenError(reason="not_connected")

        if conn.access_token_encrypted is None:
            raise WorkspaceTokenError(reason="not_connected", detail="access_token is null")

        # Chequear expiración (con buffer de 5 min)
        needs_refresh = (
            conn.expires_at is None
            or (conn.expires_at - datetime.now(UTC)) < _REFRESH_BUFFER
        )
        if needs_refresh:
            await self._refresh(conn)

        # Leer token (puede haber sido actualizado por _refresh)
        try:
            return decrypt_token(conn.access_token_encrypted)  # type: ignore[arg-type]
        except TokenCipherError as exc:
            raise WorkspaceTokenError(reason="token_corrupted", detail=str(exc)) from exc

    # ── Internals ─────────────────────────────────────────────────────────────

    async def _load_connection(self) -> UserGoogleWorkspaceConnection | None:
        result = await self._session.execute(
            select(UserGoogleWorkspaceConnection).where(
                UserGoogleWorkspaceConnection.user_id == self._user_id,
                UserGoogleWorkspaceConnection.tenant_id == self._tenant_id,
            )
        )
        return result.scalar_one_or_none()

    async def _refresh(self, conn: UserGoogleWorkspaceConnection) -> None:
        """Refresca el access_token con Google.

        Adquiere SELECT FOR UPDATE para evitar refresh concurrente.
        Hace double-check post-lock: si otro worker ya refrescó, usa ese token.
        """
        result = await self._session.execute(
            select(UserGoogleWorkspaceConnection)
            .where(
                UserGoogleWorkspaceConnection.user_id == self._user_id,
                UserGoogleWorkspaceConnection.tenant_id == self._tenant_id,
            )
            .with_for_update()
        )
        locked = result.scalar_one_or_none()

        if locked is None or not locked.is_active:
            raise WorkspaceTokenError(reason="not_connected")

        # Double-check post-lock: otro request puede haber refresheado mientras esperábamos
        if (
            locked.expires_at is not None
            and (locked.expires_at - datetime.now(UTC)) >= _REFRESH_BUFFER
        ):
            logger.info(
                "workspace.token.refresh_skipped",
                user_id=str(self._user_id),
                reason="already_refreshed_by_peer",
            )
            # Sincronizar objeto del caller con el estado actualizado
            conn.access_token_encrypted = locked.access_token_encrypted
            conn.expires_at = locked.expires_at
            return

        if not locked.refresh_token_encrypted:
            # None (nunca estuvo) o "" (bug de insert previo) → reconnect obligatorio
            raise WorkspaceTokenError(
                reason="refresh_failed",
                detail="refresh_token is null or empty — user must reconnect",
            )

        try:
            refresh_token = decrypt_token(locked.refresh_token_encrypted)
        except TokenCipherError as exc:
            raise WorkspaceTokenError(reason="token_corrupted", detail=str(exc)) from exc

        # Llamar a Google — los tokens no se loguean nunca
        token_data = await self._call_google_refresh(refresh_token)

        # Persistir nuevo token (flush, no commit — el caller lo hace)
        new_access_token = token_data["access_token"]
        expires_in: int = token_data.get("expires_in", 3600)

        locked.access_token_encrypted = encrypt_token(new_access_token)
        locked.expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
        locked.last_refresh_at = datetime.now(UTC)
        locked.last_error_code = None
        locked.last_error_at = None
        locked.updated_at = datetime.now(UTC)
        await self._session.flush()

        # Sincronizar objeto del caller
        conn.access_token_encrypted = locked.access_token_encrypted
        conn.expires_at = locked.expires_at

        logger.info(
            "workspace.token.refreshed",
            user_id=str(self._user_id),
            expires_in=expires_in,
        )

    async def _call_google_refresh(self, refresh_token: str) -> dict:  # type: ignore[type-arg]
        """POST al token endpoint de Google. Actualiza last_error_code si falla."""
        async with self._http_context() as http:
            try:
                resp = await http.post(
                    _GOOGLE_TOKEN_URL,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                        "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
                        "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
                    },
                )
            except httpx.HTTPError as exc:
                logger.error("workspace.token.google_unavailable", error=str(exc))
                await self._mark_error("google_unavailable")
                raise WorkspaceTokenError(reason="refresh_failed", detail="google_unavailable") from exc

        if resp.status_code != 200:
            error_data = resp.json() if resp.content else {}
            error_code: str = error_data.get("error", "refresh_failed")
            logger.warning(
                "workspace.token.refresh_error",
                user_id=str(self._user_id),
                error_code=error_code,
                http_status=resp.status_code,
            )
            await self._mark_error(error_code)
            raise WorkspaceTokenError(reason="refresh_failed", detail=error_code)

        return resp.json()  # type: ignore[no-any-return]

    async def _mark_error(self, error_code: str) -> None:
        """Registra last_error_code sin bloquear (best-effort, sin FOR UPDATE)."""
        result = await self._session.execute(
            select(UserGoogleWorkspaceConnection).where(
                UserGoogleWorkspaceConnection.user_id == self._user_id,
                UserGoogleWorkspaceConnection.tenant_id == self._tenant_id,
            )
        )
        conn = result.scalar_one_or_none()
        if conn:
            conn.last_error_code = error_code
            conn.last_error_at = datetime.now(UTC)
            conn.updated_at = datetime.now(UTC)
            await self._session.flush()

    @asynccontextmanager
    async def _http_context(self) -> AsyncGenerator[httpx.AsyncClient, None]:
        if self._http is not None:
            yield self._http
        else:
            async with httpx.AsyncClient(timeout=15.0) as client:
                yield client
