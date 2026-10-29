"""Estado de cuenta de un tenant (panel SUPERADMIN, F1). Puro: sin sesión ni ORM.

`tenants.status` es el estado de CUENTA, que decide el soporte. No es el estado
de la SUSCRIPCIÓN (`subscriptions.status`), que lo mueve el ciclo comercial: son
controles independientes y sus rechazos llevan códigos distintos
(`TENANT_*` vs `SUBSCRIPTION_*`). Contrato completo: `docs/plans/superadmin-panel.md`.

| Estado      | Entra | Lee | Escribe |
|-------------|-------|-----|---------|
| `ACTIVE`    | sí    | sí  | sí      |
| `TRIAL`     | sí    | sí  | sí      |
| `READ_ONLY` | sí    | sí  | no      |
| `SUSPENDED` | no    | no  | no      |

Un estado que no está en la tabla —incluido `None`— **falla cerrado**: se trata
como `SUSPENDED`. Lo contrario dejaría entrar a un tenant por un valor mal
escrito.
"""

from __future__ import annotations

from enum import StrEnum

ACTIVE = "ACTIVE"
TRIAL = "TRIAL"
READ_ONLY = "READ_ONLY"
SUSPENDED = "SUSPENDED"

#: Los estados que el panel puede asignar.
ESTADOS_ASIGNABLES: tuple[str, ...] = (ACTIVE, TRIAL, READ_ONLY, SUSPENDED)

#: Estados en los que "corre" el tenant: jobs de score, momentum, integridad y
#: mail semanal. Una sola definición, porque cuatro jobs filtraban cada uno con
#: la suya (`"active"` en minúsculas incluido) y no coincidían entre sí.
ESTADOS_OPERATIVOS: tuple[str, ...] = (ACTIVE, TRIAL)

TENANT_SUSPENDED_CODE = "TENANT_SUSPENDED"
TENANT_READ_ONLY_CODE = "TENANT_READ_ONLY"

MENSAJE_SUSPENDIDA = (
    "Tu cuenta está suspendida. Escribinos para reactivarla: los datos están a salvo."
)
MENSAJE_SOLO_LECTURA = (
    "Tu cuenta está en modo solo lectura: podés consultar tus datos, pero no cargar ni "
    "modificar nada hasta que se reactive."
)

#: Roles que el estado de cuenta no alcanza: el soporte tiene que poder entrar a
#: ver y reactivar justo el tenant que bloqueó (el suyo propio incluido). Se
#: compara contra el rol de la BASE, nunca contra el claim del JWT.
ROLES_INMUNES: frozenset[str] = frozenset({"SUPERADMIN"})

_METODOS_DE_LECTURA = frozenset({"GET", "HEAD", "OPTIONS"})

#: (método, plantilla de ruta) que NO escriben aunque no sean GET, y que pasan
#: también con la cuenta en solo lectura. Es una lista cerrada: un `POST` sin
#: clasificar se bloquea (denegar por defecto), igual que la ruta de un cajero.
#: `/auth/logout` pasa también suspendida: salir no es operar.
PERMITIDAS_EN_SOLO_LECTURA: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/api/v1/auth/logout"),
        ("POST", "/api/v1/health-scores/{snapshot_id}/export"),
    }
)
PERMITIDAS_SUSPENDIDA: frozenset[tuple[str, str]] = frozenset(
    {("POST", "/api/v1/auth/logout")}
)


class NivelAcceso(StrEnum):
    COMPLETO = "COMPLETO"
    SOLO_LECTURA = "SOLO_LECTURA"
    DENEGADO = "DENEGADO"


def normalizar(status: str | None) -> str | None:
    """`" active "` y `"ACTIVE"` son el mismo estado; `None` y `""` no son ninguno."""
    if status is None:
        return None
    limpio = status.strip().upper()
    return limpio or None


def nivel_de_acceso(status: str | None) -> NivelAcceso:
    estado = normalizar(status)
    if estado in (ACTIVE, TRIAL):
        return NivelAcceso.COMPLETO
    if estado == READ_ONLY:
        return NivelAcceso.SOLO_LECTURA
    return NivelAcceso.DENEGADO


def puede_autenticarse(status: str | None, role_code: str | None) -> bool:
    """¿Puede emitirse un token (login, refresh, verify-email, OAuth)?

    Solo-lectura entra: lo que le queda vedado es escribir, no ver sus datos.
    """
    if role_code in ROLES_INMUNES:
        return True
    return nivel_de_acceso(status) is not NivelAcceso.DENEGADO


def bloqueo_de_peticion(
    status: str | None,
    role_code: str | None,
    method: str,
    route_path: str,
) -> str | None:
    """Código de rechazo de esta petición según el estado de cuenta, o `None`.

    `route_path` es la plantilla de FastAPI (`route.path`), no la URL concreta.
    """
    if role_code in ROLES_INMUNES:
        return None
    clave = (method.upper(), route_path)
    nivel = nivel_de_acceso(status)
    if nivel is NivelAcceso.COMPLETO:
        return None
    if nivel is NivelAcceso.DENEGADO:
        return None if clave in PERMITIDAS_SUSPENDIDA else TENANT_SUSPENDED_CODE
    if method.upper() in _METODOS_DE_LECTURA or clave in PERMITIDAS_EN_SOLO_LECTURA:
        return None
    return TENANT_READ_ONLY_CODE


def mensaje_de(codigo: str) -> str:
    return MENSAJE_SOLO_LECTURA if codigo == TENANT_READ_ONLY_CODE else MENSAJE_SUSPENDIDA
