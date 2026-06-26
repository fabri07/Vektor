"""PinService — step-up auth con PIN de 4 dígitos.

Segundo factor para acciones sensibles (editar/borrar datos ya cargados,
configuraciones, releer archivos, forzar baja, retiro de ganancias). El PIN es
**por usuario**; el OWNER siempre puede tener PIN, las sub-cuentas necesitan el
permiso ``can_modify_sensitive``.

Diseño de seguridad:
- Hash bcrypt reusando ``hash_password``/``verify_password`` (rounds=12).
- Ventana de verificación en Redis (TTL 10 min): una verificación habilita
  múltiples mutaciones dentro de la ventana (no se pide PIN por celda).
- **Fail-closed:** si Redis falla al leer la ventana, se trata como NO
  verificado (nunca permite). Si falla al escribir tras un PIN correcto, se
  devuelve error (no se finge éxito).
- Lockout por usuario tras 5 intentos fallidos (15 min).
- Mensajes genéricos: nunca se revela si el PIN existe; el valor del PIN nunca
  se loguea ni se audita.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from app.observability.logger import get_logger
from app.persistence.models.user import User
from app.persistence.repositories.user_repository import UserRepository
from app.utils.security import hash_password, verify_password

logger = get_logger(__name__)

WINDOW_TTL_SECONDS = 600  # 10 min
LOCK_TTL_SECONDS = 900  # 15 min
MAX_ATTEMPTS = 5


# ── Excepciones de control de flujo (el router las traduce a HTTP) ──────────────


class PinError(Exception):
    """Base para errores de PIN. ``detail`` es genérico, apto para el cliente."""

    detail = "PIN inválido."


class PinInvalidError(PinError):
    detail = "PIN inválido."


class PinNotSetError(PinError):
    detail = "PIN inválido."  # genérico: no revelar que no está configurado


class PinAlreadySetError(PinError):
    detail = "El PIN ya está configurado. Usá cambiar PIN."


class PinLockedError(PinError):
    detail = "Demasiados intentos. Probá de nuevo en unos minutos."


class PinMismatchError(PinError):
    detail = "Los PIN no coinciden."


class PinServiceUnavailableError(PinError):
    detail = "Servicio de verificación no disponible. Reintentá en unos minutos."


class PinService:
    def __init__(self, redis: Redis, session: AsyncSession | None = None) -> None:
        self._redis = redis
        self._session = session

    # ── Keys ────────────────────────────────────────────────────────────────
    @staticmethod
    def _window_key(tenant_id: UUID, user_id: UUID) -> str:
        return f"pin:verified:{tenant_id}:{user_id}"

    @staticmethod
    def _attempts_key(tenant_id: UUID, user_id: UUID) -> str:
        return f"pin:attempts:{tenant_id}:{user_id}"

    @staticmethod
    def _lock_key(tenant_id: UUID, user_id: UUID) -> str:
        return f"pin:lock:{tenant_id}:{user_id}"

    # ── Ventana (usado por las dependencies de gating) ────────────────────────
    async def is_window_valid(self, tenant_id: UUID, user_id: UUID) -> bool:
        """True si hay ventana de PIN vigente. **Fail-closed:** error de Redis → False."""
        try:
            return bool(await self._redis.exists(self._window_key(tenant_id, user_id)))
        except RedisError:
            logger.warning("pin.window.redis_error", tenant_id=str(tenant_id))
            return False

    async def invalidate_window(self, tenant_id: UUID, user_id: UUID) -> None:
        """Borra la ventana (logout, cambio de PIN/password). Best-effort."""
        try:
            await self._redis.delete(self._window_key(tenant_id, user_id))
        except RedisError:
            logger.warning("pin.window.invalidate_error", tenant_id=str(tenant_id))

    # ── Status ────────────────────────────────────────────────────────────────
    async def get_status(self, user: User) -> dict[str, bool]:
        pin_set = user.pin_hash is not None
        verified = await self.is_window_valid(user.tenant_id, user.user_id)
        allowed = user.role_code == "OWNER" or user.can_modify_sensitive
        return {"pin_set": pin_set, "verified": verified, "must_set": allowed and not pin_set}

    # ── Setup / change / reset ────────────────────────────────────────────────
    async def setup_pin(self, user: User, pin: str, pin_confirm: str) -> None:
        if user.pin_hash is not None:
            raise PinAlreadySetError()
        if pin != pin_confirm:
            raise PinMismatchError()
        await self._persist_pin(user, pin)
        logger.info("pin.setup", user_id=str(user.user_id))

    async def change_pin(
        self, user: User, current_pin: str, new_pin: str, new_pin_confirm: str
    ) -> None:
        tenant_id, user_id = user.tenant_id, user.user_id
        # Mismo lockout que verify: este endpoint también valida el PIN actual y
        # sería un oráculo de fuerza bruta sin el contador de intentos.
        await self._assert_not_locked(tenant_id, user_id)
        if user.pin_hash is None:
            raise PinNotSetError()
        if not verify_password(current_pin, user.pin_hash):
            await self._register_failed_attempt(tenant_id, user_id)
            raise PinInvalidError()
        if new_pin != new_pin_confirm:
            raise PinMismatchError()
        await self._clear_attempts(tenant_id, user_id)
        await self._persist_pin(user, new_pin)
        await self.invalidate_window(tenant_id, user_id)
        logger.info("pin.change", user_id=str(user_id))

    async def reset_pin(
        self, user: User, password: str, new_pin: str, new_pin_confirm: str
    ) -> None:
        """Fallback ante PIN olvidado: valida la contraseña de la cuenta."""
        tenant_id, user_id = user.tenant_id, user.user_id
        # Throttle: valida la contraseña de la cuenta → sería un oráculo de fuerza
        # bruta de password sin el contador de intentos.
        await self._assert_not_locked(tenant_id, user_id)
        if not verify_password(password, user.password_hash):
            await self._register_failed_attempt(tenant_id, user_id)
            raise PinInvalidError()
        if new_pin != new_pin_confirm:
            raise PinMismatchError()
        await self._clear_attempts(tenant_id, user_id)
        await self._persist_pin(user, new_pin)
        await self.invalidate_window(tenant_id, user_id)
        logger.info("pin.reset", user_id=str(user_id))

    async def _persist_pin(self, user: User, pin: str) -> None:
        if self._session is None:
            raise RuntimeError("PinService needs a session to persist the PIN.")
        user.pin_hash = hash_password(pin)
        user.pin_set_at = datetime.now(UTC)
        await UserRepository(self._session).save(user)

    # ── Verify (abre la ventana) ──────────────────────────────────────────────
    async def verify(self, user: User, pin: str) -> None:
        """Verifica el PIN y abre la ventana. Lanza ``PinError`` ante cualquier fallo."""
        tenant_id, user_id = user.tenant_id, user.user_id

        # 1) lockout
        await self._assert_not_locked(tenant_id, user_id)

        # 2) PIN configurado
        if user.pin_hash is None:
            await self._register_failed_attempt(tenant_id, user_id)
            raise PinNotSetError()

        # 3) verificar
        if not verify_password(pin, user.pin_hash):
            await self._register_failed_attempt(tenant_id, user_id)
            raise PinInvalidError()

        # 4) éxito: limpiar intentos + abrir ventana (fail-closed en la escritura)
        await self._clear_attempts(tenant_id, user_id)
        try:
            await self._redis.set(
                self._window_key(tenant_id, user_id), "1", ex=WINDOW_TTL_SECONDS
            )
        except RedisError as exc:
            raise PinServiceUnavailableError() from exc

    async def _assert_not_locked(self, tenant_id: UUID, user_id: UUID) -> None:
        try:
            if await self._redis.exists(self._lock_key(tenant_id, user_id)):
                raise PinLockedError()
        except RedisError as exc:
            raise PinServiceUnavailableError() from exc

    async def _clear_attempts(self, tenant_id: UUID, user_id: UUID) -> None:
        try:
            await self._redis.delete(self._attempts_key(tenant_id, user_id))
        except RedisError:
            logger.warning("pin.attempt.clear_error", tenant_id=str(tenant_id))

    async def _register_failed_attempt(self, tenant_id: UUID, user_id: UUID) -> None:
        try:
            count = await self._redis.incr(self._attempts_key(tenant_id, user_id))
            # TTL en CADA intento (ventana deslizante): evita que un fallo de
            # `expire` tras el primer `incr` deje la key sin expiración.
            await self._redis.expire(self._attempts_key(tenant_id, user_id), LOCK_TTL_SECONDS)
            if count >= MAX_ATTEMPTS:
                await self._redis.set(
                    self._lock_key(tenant_id, user_id), "1", ex=LOCK_TTL_SECONDS
                )
        except RedisError:
            # No bloqueamos el flujo de error por un fallo de Redis al contar.
            logger.warning("pin.attempt.redis_error", tenant_id=str(tenant_id))
