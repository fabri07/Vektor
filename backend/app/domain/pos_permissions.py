"""Qué puede hacer un cajero (rol ``CASHIER``) y a qué rutas llega.

Dos listas cerradas, las dos puras:

* :data:`CASHIER_ALLOWED_ROUTES` — las rutas que un cajero alcanza. Cualquier
  otra da 403 en ``get_current_user``. Es **denegar por defecto**: 200 de las
  218 rutas autenticadas sólo piden un usuario logueado, y una ruta nueva no
  puede nacer abierta para el cajero porque alguien se olvidó de cerrarla.
* :class:`PosPermission` — lo que el dueño le habilita a cada cajero adentro de
  esas rutas (``users.pos_permissions``).

OWNER y ADMIN tienen todos los permisos de caja: nada de lo que ya hacían cambia.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

CASHIER_ROLE = "CASHIER"
ROLES_CON_CAJA_COMPLETA = frozenset({"OWNER", "ADMIN"})


class PosPermission(StrEnum):
    DISCOUNT = "discount"
    VOID_TICKET = "void_ticket"
    FIADO = "fiado"
    LEARN_BARCODE = "learn_barcode"


#: (método, plantilla de ruta) que un cajero puede llamar. La plantilla es la de
#: FastAPI (``route.path``), no la URL concreta.
_P = "/api/v1"
CASHIER_ALLOWED_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        # Sesión: sin esto no puede ni quedarse logueado. (`/auth/refresh` no
        # está porque trabaja con el refresh token, no con get_current_user.)
        ("GET", f"{_P}/auth/me"),
        ("POST", f"{_P}/auth/logout"),
        ("POST", f"{_P}/auth/change-password"),
        ("GET", f"{_P}/auth/pin/status"),
        ("POST", f"{_P}/auth/pin/setup"),
        ("POST", f"{_P}/auth/pin/verify"),
        ("POST", f"{_P}/auth/pin/change"),
        ("POST", f"{_P}/auth/pin/reset"),
        ("GET", f"{_P}/users/me"),
        ("PATCH", f"{_P}/users/me"),
        # Caja.
        ("GET", f"{_P}/pos/catalog"),
        ("GET", f"{_P}/pos/customers"),
        ("GET", f"{_P}/products/lookup"),
        ("POST", f"{_P}/pos/operations"),
        ("POST", f"{_P}/pos/operations/{{operation_id}}/void"),
        ("POST", f"{_P}/products/{{product_id}}/barcode"),
    }
)


def cashier_can_reach(method: str, route_path: str) -> bool:
    return (method.upper(), route_path) in CASHIER_ALLOWED_ROUTES


def normalize_pos_permissions(raw: Any) -> dict[str, bool]:
    """Lo que se GUARDA: sólo claves conocidas, sólo con valor booleano ``True``.

    ``"true"``, ``1`` y ``None`` no son ``True``: un JSON mal armado no puede
    terminar habilitando a nadie. Las claves desconocidas se descartan.
    """
    if not isinstance(raw, Mapping):
        return {}
    return {p.value: True for p in PosPermission if raw.get(p.value) is True}


def effective_pos_permissions(
    role_code: str, stored: Any
) -> frozenset[PosPermission]:
    """Los permisos de caja que rigen para un usuario."""
    if role_code in ROLES_CON_CAJA_COMPLETA:
        return frozenset(PosPermission)
    if role_code == CASHIER_ROLE:
        return frozenset(PosPermission(k) for k in normalize_pos_permissions(stored))
    return frozenset()


def has_pos_permission(role_code: str, stored: Any, permiso: PosPermission) -> bool:
    return permiso in effective_pos_permissions(role_code, stored)
