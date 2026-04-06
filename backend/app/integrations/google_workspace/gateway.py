"""GoogleWorkspaceGateway — punto de entrada único para la integración Workspace.

AgentSupplier (y cualquier otro caller) instancia este gateway y nunca accede
directamente a TokenManager o GmailClient.

Responsabilidades:
  - is_connected()  → chequea si el usuario tiene una conexión Workspace activa
  - gmail()         → obtiene GmailClient con token vigente
  - disconnect()    → soft revoke (local siempre; remoto best-effort)

Fase futura (no implementada aquí):
  - drive() / sheets() / calendar()

Invariantes de seguridad:
  - tenant_id enforced en todas las queries
  - Token revocado (revoked_at is not None) → WorkspaceTokenError("not_connected")
  - Revoke remoto fallido → se loguea y se continúa (fail-closed local)
  - Tokens cifrados se anulan en disconnect (minimizar retención)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import httpx
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.google_workspace.exceptions import (
    InsufficientScopeError,
    WorkspaceTokenError,
)
from app.integrations.google_workspace.gmail_client import GmailClient
from app.integrations.google_workspace.token_manager import TokenManager
from app.application.security.token_cipher import TokenCipherError, decrypt_token
from app.observability.logger import get_logger
from app.persistence.models.user_google_workspace import UserGoogleWorkspaceConnection

logger = get_logger(__name__)

_GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"


class GoogleWorkspaceGateway:
    """Gateway de Workspace para un usuario autenticado.

    Args:
        session:     AsyncSession abierta (sin commit — el caller lo hace).
        redis:       Instancia Redis (reservado para uso futuro, e.g. cache de scopes).
        user_id:     UUID del usuario autenticado (del JWT).
        tenant_id:   UUID del tenant (enforced en todas las queries).
        http_client: httpx.AsyncClient inyectado en tests; None usa uno nuevo en producción.
    """

    def __init__(
        self,
        session: AsyncSession,
        redis: Redis,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._session = session
        self._redis = redis
        self._user_id = user_id
        self._tenant_id = tenant_id
        self._http = http_client
        self._token_manager = TokenManager(session, user_id, tenant_id, http_client)

    # ── API pública ───────────────────────────────────────────────────────────

    async def is_connected(self) -> bool:
        """True si el usuario tiene una conexión Workspace activa (no revocada)."""
        conn = await self._load_connection()
        return conn is not None and conn.is_active

    async def gmail(self) -> GmailClient:
        """Retorna un GmailClient con token vigente.

        Refresca el token automáticamente si está por vencer.

        Raises:
            WorkspaceTokenError — si no hay conexión activa, el refresh falla,
                                  o el scope es insuficiente.
        """
        try:
            token = await self._token_manager.get_valid_access_token()
        except WorkspaceTokenError:
            raise
        return GmailClient(access_token=token, http_client=self._http)

    async def run_gmail(self, coro):  # type: ignore[no-untyped-def]
        """Helper: ejecuta una coroutine de GmailClient capturando InsufficientScopeError.

        Uso:
            result = await gateway.run_gmail(
                (await gateway.gmail()).list_messages(query="from:proveedor")
            )

        Convierte InsufficientScopeError en WorkspaceTokenError(reason="insufficient_scope")
        y actualiza last_error_code en DB antes de propagar.
        """
        try:
            return await coro
        except InsufficientScopeError as exc:
            await self._mark_insufficient_scope()
            raise WorkspaceTokenError(reason="insufficient_scope", detail=str(exc)) from exc

    async def disconnect(self) -> None:
        """Soft revoke de la conexión Workspace.

        - Idempotente: si ya estaba revocada, retorna sin hacer nada.
        - Revoke remoto best-effort: si Google no responde, se continúa igual.
        - Tokens cifrados se anulan (minimizar retención).
        - La fila NO se borra — preserva auditoría (connected_at, revoked_at, etc.).
        """
        result = await self._session.execute(
            select(UserGoogleWorkspaceConnection)
            .where(
                UserGoogleWorkspaceConnection.user_id == self._user_id,
                UserGoogleWorkspaceConnection.tenant_id == self._tenant_id,
            )
            .with_for_update()
        )
        conn = result.scalar_one_or_none()

        if conn is None or conn.revoked_at is not None:
            logger.info(
                "workspace.disconnect.idempotent",
                user_id=str(self._user_id),
                already_revoked=(conn is not None),
            )
            return

        # Best-effort remote revoke
        await self._remote_revoke(conn)

        # Local revoke — siempre
        conn.revoked_at = datetime.now(UTC)
        conn.last_error_code = "revoked"
        conn.access_token_encrypted = None
        conn.refresh_token_encrypted = None
        conn.updated_at = datetime.now(UTC)
        await self._session.flush()

        logger.info("workspace.disconnect.success", user_id=str(self._user_id))

    # ── Internals ─────────────────────────────────────────────────────────────

    async def _load_connection(self) -> UserGoogleWorkspaceConnection | None:
        result = await self._session.execute(
            select(UserGoogleWorkspaceConnection).where(
                UserGoogleWorkspaceConnection.user_id == self._user_id,
                UserGoogleWorkspaceConnection.tenant_id == self._tenant_id,
            )
        )
        return result.scalar_one_or_none()

    async def _remote_revoke(self, conn: UserGoogleWorkspaceConnection) -> None:
        """Intenta revocar el token en Google. Loguea si falla, no propaga."""
        if not conn.access_token_encrypted:
            return
        try:
            token = decrypt_token(conn.access_token_encrypted)
            async with httpx.AsyncClient(timeout=5.0) as http:
                resp = await http.post(_GOOGLE_REVOKE_URL, params={"token": token})
                if resp.status_code not in (200, 400):
                    # 400 = token ya inválido — OK para nosotros
                    logger.warning(
                        "workspace.disconnect.remote_revoke_unexpected",
                        user_id=str(self._user_id),
                        status=resp.status_code,
                    )
        except (TokenCipherError, httpx.HTTPError, Exception) as exc:
            logger.warning(
                "workspace.disconnect.remote_revoke_failed",
                user_id=str(self._user_id),
                error=str(exc),
            )

    async def _mark_insufficient_scope(self) -> None:
        """Actualiza last_error_code='insufficient_scope' en DB (best-effort)."""
        result = await self._session.execute(
            select(UserGoogleWorkspaceConnection).where(
                UserGoogleWorkspaceConnection.user_id == self._user_id,
                UserGoogleWorkspaceConnection.tenant_id == self._tenant_id,
            )
        )
        conn = result.scalar_one_or_none()
        if conn:
            conn.last_error_code = "insufficient_scope"
            conn.last_error_at = datetime.now(UTC)
            conn.updated_at = datetime.now(UTC)
            await self._session.flush()
