"""Cambios de rol y de permisos del equipo, con sus reglas y su auditoría (B5).

Existe para que el estado GUARDADO de un usuario cumpla las reglas del cajero,
no sólo lo que el guard de rutas deja pasar:

* Un CASHIER nunca tiene ``can_modify_sensitive``: esa bandera abre el
  PATCH/DELETE genérico de datos, que un cajero no debe alcanzar.
* Pasar a alguien a cajero arranca SIN permisos de caja y le cierra la ventana
  de PIN que pudiera tener abierta como admin.
* Dejar de ser cajero borra sus permisos de caja: si vuelve a serlo, no
  reaparecen los viejos por accidente.

Cada cambio queda en ``decision_audit_log`` con el antes y el después.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.pin_service import PinService
from app.domain.pos_permissions import CASHIER_ROLE, normalize_pos_permissions
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.user import User


def _estado(user: User) -> dict[str, Any]:
    return {
        "role_code": user.role_code,
        "can_modify_sensitive": bool(user.can_modify_sensitive),
        "pos_permissions": dict(user.pos_permissions or {}),
    }


def _auditar(
    session: AsyncSession,
    actor: User,
    target: User,
    tipo: str,
    antes: dict[str, Any],
) -> None:
    session.add(
        DecisionAuditLog(
            tenant_id=target.tenant_id,
            decision_type=tipo,
            decision_data={
                "record_type": "user",
                "record_id": str(target.user_id),
                "before": antes,
                "after": _estado(target),
                "source": "ui",
            },
            triggered_by="ui:team",
            actor_user_id=actor.user_id,
            context={"endpoint": "team"},
            created_at=datetime.now(UTC),
        )
    )


def aplicar_rol(user: User, nuevo_rol: str) -> bool:
    """Deja el usuario en ``nuevo_rol`` con el estado que ese rol exige.

    Devuelve si hay que cerrar la ventana de PIN (pasó a cajero). Pura sobre el
    objeto: no toca la sesión ni Redis, para poder usarse también en el alta.
    """
    anterior = user.role_code
    user.role_code = nuevo_rol
    if nuevo_rol == CASHIER_ROLE:
        user.can_modify_sensitive = False
        if anterior != CASHIER_ROLE:
            user.pos_permissions = {}
            return True
        return False
    if anterior == CASHIER_ROLE:
        user.pos_permissions = {}
    return False


async def cambiar_rol(
    session: AsyncSession, pin: PinService, actor: User, target: User, nuevo_rol: str
) -> None:
    if nuevo_rol == target.role_code:
        return
    antes = _estado(target)
    cerrar_pin = aplicar_rol(target, nuevo_rol)
    if cerrar_pin:
        await pin.invalidate_window(target.tenant_id, target.user_id)
    _auditar(session, actor, target, "TEAM_ROLE_CHANGED", antes)


def cambiar_permisos_pos(
    session: AsyncSession, actor: User, target: User, permisos: Any
) -> None:
    """Guarda sólo claves conocidas con ``True``. El caller ya verificó el rol."""
    antes = _estado(target)
    target.pos_permissions = normalize_pos_permissions(permisos)
    _auditar(session, actor, target, "TEAM_POS_PERMISSIONS_CHANGED", antes)


def cambiar_permiso_general(
    session: AsyncSession, actor: User, target: User, valor: bool
) -> None:
    antes = _estado(target)
    target.can_modify_sensitive = valor
    _auditar(session, actor, target, "TEAM_MODIFY_PERMISSION_CHANGED", antes)
