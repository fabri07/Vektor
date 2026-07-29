"""
FastAPI dependency injection helpers.

Every business endpoint must inject `get_current_tenant` and
`get_current_user` to enforce authentication and tenant isolation.

JWT payload expected keys: sub (user_id), tenant_id, role_code.
"""

from collections.abc import Callable
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import maintenance_lock_service
from app.application.services.pin_service import PinService
from app.config.settings import get_settings
from app.observability.logger import bind_request_context, get_logger
from app.persistence.db.redis_client import get_redis
from app.persistence.db.session import get_db_session
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.persistence.repositories.tenant_repository import TenantRepository
from app.persistence.repositories.user_repository import UserRepository
from app.utils.security import decode_access_token

# Código que el frontend reconoce para abrir el modal de PIN y reintentar.
PIN_REQUIRED_CODE = "PIN_REQUIRED"

_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_db_session),
) -> User:
    """Decode JWT and return the authenticated user. Raises 401 if invalid."""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = credentials.credentials
    payload = decode_access_token(token)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id: str | None = payload.get("sub")
    tenant_id: str | None = payload.get("tenant_id")
    if not user_id or not tenant_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Malformed token.")

    repo = UserRepository(session)
    user = await repo.get_by_id(UUID(user_id), UUID(tenant_id))
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found.")

    # Bind tenant_id and user_id into structlog context for this request
    bind_request_context(tenant_id=user.tenant_id, user_id=user.user_id)

    return user


def get_current_tenant_id(current_user: User = Depends(get_current_user)) -> UUID:
    """Return the tenant_id of the authenticated user. Propagates to all business queries."""
    return current_user.tenant_id


async def get_current_tenant(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> Tenant:
    """Return the tenant for the authenticated user. Raises 403 if suspended."""
    repo = TenantRepository(session)
    tenant = await repo.get_by_id(current_user.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found.")
    if tenant.status not in ("ACTIVE", "TRIAL"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Tenant is {tenant.status}.",
        )
    return tenant


def require_role(*roles: str) -> Callable:  # type: ignore[type-arg]
    """Dependency factory that enforces role-based access. Pass uppercase role codes."""

    async def _check(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role_code not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{current_user.role_code}' is not allowed to perform this action.",
            )
        return current_user

    return _check


# ── Endpoints públicos (sin auth) ───────────────────────────────────────────────

#: Código que devuelve `POST /auth/register` cuando el registro abierto está
#: apagado. Lo lee el frontend para mostrar el copy de "el acceso ahora se pide"
#: en lugar de un error genérico.
REGISTRATION_CLOSED_CODE = "registration_closed"


#: Header que setea el edge de Railway: UN solo valor, sin la cadena ambigua de
#: `X-Forwarded-For` (que puede traer varias IPs y cuyo primer valor no se puede
#: creer sin una política de proxies confiables).
_HEADER_IP_REAL = "x-real-ip"

#: Key del limiter cuando no se pudo determinar la IP. Vive acá y NO en
#: `client_ip` a propósito: `client_ip` devuelve `None` para decir "no la sé", y
#: `hash_ip(None)` es `None`. Si `client_ip` devolviera este centinela, el
#: `ip_hash` guardado sería el hash de un literal inventado, indistinguible de
#: una IP real y compartido por todos los clientes sin IP.
_SIN_IP = "unknown"

#: Se avisa UNA sola vez por proceso: es una condición de configuración del
#: despliegue, no un evento por request — loguearla en cada uno inundaría.
_aviso_sin_x_real_ip_emitido = False


def _avisar_sin_x_real_ip() -> None:
    """Denuncia una sola vez que el edge no manda `X-Real-IP` en producción.

    Nunca loguea la IP ni el hash: solo que la suposición del diseño no se
    cumple, que es lo único que hace falta saber para ir a mirar.
    """
    global _aviso_sin_x_real_ip_emitido
    if _aviso_sin_x_real_ip_emitido:
        return
    _aviso_sin_x_real_ip_emitido = True
    get_logger(__name__).warning(
        "client_ip.sin_x_real_ip",
        detalle=(
            "En producción no llegó X-Real-IP: el rate limit y el ip_hash caen "
            "a request.client.host, que detrás de un proxy es la IP del edge e "
            "igual para todos los visitantes."
        ),
    )


def client_ip(request: Request) -> str | None:
    """IP del cliente. **Definición única** de "el cliente" en toda la app.

    La comparten el `ip_hash` anti-abuso de los dos formularios públicos
    anónimos (contacto y solicitud de acceso) y la key del rate limiter global
    (`rate_limit_key`). Antes eran dos nociones distintas dentro del MISMO
    handler: el `ip_hash` leía `X-Forwarded-For` y el `@limiter.limit("5/hour")`
    usaba `get_remote_address`, que lo ignora — detrás del edge de Railway eso
    podía volver el 5/hour un techo GLOBAL del único embudo de alta.

    La confianza en el header está atada al DESPLIEGUE, no al header: solo se
    lee `X-Real-IP` en producción, porque cualquiera que alcance uvicorn directo
    puede inventarlo. Fuera de producción (dev y tests) se usa siempre
    `request.client.host`.

    Sin header, degrada a `request.client.host`. Para el limiter eso es
    exactamente lo que hacía `get_remote_address` y no empeora nada; para el
    `ip_hash` **sí** sería una regresión, porque antes leía `X-Forwarded-For`
    incondicionalmente: si el edge mandara solo XFF y no `X-Real-IP`, la columna
    pasaría a guardar un único hash (el del edge) para todos los visitantes, y
    eso no se nota mirando los datos — un hash de la IP del proxy es
    indistinguible de uno de cliente.

    Por eso la suposición se autodenuncia: en producción, la primera vez que
    falte `X-Real-IP` se loguea una advertencia (sin IP ni hash). Si aparece en
    los logs de prod, hay que revisar qué manda el edge — no queda pendiente de
    que alguien se acuerde de ir a mirar.

    `None` cuando no hay forma de saberla: no se inventa un valor.
    """
    if get_settings().is_production:
        real = request.headers.get(_HEADER_IP_REAL)
        if real and real.strip():
            return real.strip()
        _avisar_sin_x_real_ip()
    return request.client.host if request.client else None


def rate_limit_key(request: Request) -> str:
    """Key del rate limiter global — la MISMA IP que hashea el `ip_hash`.

    `slowapi` exige un `str`, así que acá (y solo acá) el "no la sé" se colapsa
    a un centinela: todos los requests sin IP comparten cubeta, que es el
    comportamiento conservador y equivale a lo que ya hacía `get_remote_address`.
    """
    ip = client_ip(request)
    return ip if ip is not None else _SIN_IP


def require_open_registration() -> None:
    """Corta con 410 el registro abierto mientras el flag esté OFF.

    El alta de cuentas pasa por `POST /access-requests` (solicitud + aprobación
    manual). El endpoint viejo NO se borra: conserva la ruta y devuelve un 410 con
    código estable, así un bundle desactualizado del frontend recibe una señal
    accionable en vez de un 404. Prender `ENABLE_OPEN_REGISTRATION` restituye el
    comportamiento histórico completo (rollback de una línea).

    Va como `dependencies=[...]` del endpoint —no como primera línea del cuerpo—
    para que se resuelva ANTES de validar el body: un payload viejo tiene que ver
    el 410, no un 422 de esquema.

    ⚠️ **Alcance: solo `POST /auth/register`.** NO gatear `POST /onboarding/submit`:
    la encuesta se partió en dos y esa mitad —los 6 números financieros— se pide
    DESPUÉS de aprobar, en el primer login, detrás de JWT y sin crear cuentas. No
    es parte del registro abierto.
    """
    if not get_settings().ENABLE_OPEN_REGISTRATION:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=REGISTRATION_CLOSED_CODE,
        )


# ── Step-up auth (PIN) ──────────────────────────────────────────────────────────


async def _require_pin_window(current_user: User, redis: Redis) -> None:
    """Lanza 428 PIN_REQUIRED si no hay ventana de PIN vigente. Fail-closed."""
    pin_service = PinService(redis)
    if not await pin_service.is_window_valid(current_user.tenant_id, current_user.user_id):
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail=PIN_REQUIRED_CODE,
        )


async def require_modify_access(
    current_user: User = Depends(get_current_user),
    redis: Redis = Depends(get_redis),
) -> User:
    """Gate para editar/borrar datos ya cargados + configuraciones.

    (a) OWNER o sub-cuenta con ``can_modify_sensitive``, si no → 403.
    (b) ventana de PIN vigente, si no → 428 PIN_REQUIRED.
    """
    if not (current_user.role_code == "OWNER" or current_user.can_modify_sensitive):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No tenés permiso para modificar datos. Pedíselo al dueño de la cuenta.",
        )
    await _require_pin_window(current_user, redis)
    return current_user


async def require_owner_stepup(
    current_user: User = Depends(get_current_user),
    redis: Redis = Depends(get_redis),
) -> User:
    """Gate para acciones exclusivas del OWNER (forzar baja con historial,
    reactivar, permisos de equipo, gestión de usuarios).

    (a) rol OWNER estricto, si no → 403.
    (b) ventana de PIN vigente, si no → 428 PIN_REQUIRED.
    """
    if current_user.role_code != "OWNER":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Solo el dueño de la cuenta puede realizar esta acción.",
        )
    await _require_pin_window(current_user, redis)
    return current_user


# ── Mantenimiento (F3-T3 — dedup de productos) ──────────────────────────────────


async def ensure_tenant_not_under_maintenance(
    tenant_id: UUID = Depends(get_current_tenant_id),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    """Guard HTTP 423 (fast-fail de UX) mientras corre la deduplicación del tenant.

    NO es la garantía de exclusión mutua real — esa la da el advisory lock
    transaccional (``maintenance_lock_service.acquire_write_lock_shared``) que
    cada write boundary toma antes de mutar. Este guard solo evita que el
    cliente mande un request que sabemos de entrada que va a esperar/fallar;
    sin el advisory lock quedaría una carrera TOCTOU (un request que ya pasó
    este chequeo pero escribe mientras el dedup fusiona).
    """
    if await maintenance_lock_service.is_locked(session, tenant_id):
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail=(
                "La cuenta está en mantenimiento (deduplicación en curso). "
                "Reintentá en unos minutos."
            ),
        )
