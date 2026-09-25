"""Terminales de caja (B6): habilitar, listar, dar de baja y validar el header.

La credencial identifica la PC, no a la persona: se valida SIEMPRE junto con el
login del usuario y contra su tenant. Un cajero sólo escribe desde una terminal
habilitada; el dueño y el admin pueden operar desde el navegador sin terminal.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.pos_permissions import CASHIER_ROLE
from app.domain.pos_terminal import generar_secreto, hash_de_secreto, secreto_coincide
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.pos_terminal import PosTerminal
from app.persistence.models.user import User


def _prohibido(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN, detail={"code": code, "message": message}
    )


async def resolver_terminal(
    session: AsyncSession, tenant_id: uuid.UUID, secreto: str | None
) -> PosTerminal | None:
    """La terminal del header, o ``None`` si no vino header.

    - Secreto desconocido, o de otro negocio → 403 ``TERMINAL_UNKNOWN``.
    - Terminal dada de baja → 403 ``TERMINAL_DISABLED``: es el código que la caja
      offline (B7) lee para dejar sus pendientes en "requiere autenticación".
    """
    if secreto is None:
        return None
    terminal = (
        await session.execute(
            select(PosTerminal).where(
                PosTerminal.tenant_id == tenant_id,
                PosTerminal.credential_hash == hash_de_secreto(secreto),
            )
        )
    ).scalar_one_or_none()
    if terminal is None or not secreto_coincide(secreto, terminal.credential_hash):
        raise _prohibido(
            "TERMINAL_UNKNOWN", "Esta PC no está habilitada como caja de este negocio."
        )
    if terminal.disabled_at is not None:
        raise _prohibido(
            "TERMINAL_DISABLED", "Esta caja fue dada de baja. Pedile al dueño que la habilite."
        )
    terminal.last_seen_at = datetime.now(UTC)
    return terminal


def exigir_terminal_si_es_cajero(user: User, terminal: PosTerminal | None) -> None:
    """Un cajero sólo escribe desde una caja habilitada; el dueño, desde donde sea."""
    if user.role_code == CASHIER_ROLE and terminal is None:
        raise _prohibido(
            "TERMINAL_REQUIRED",
            "Para cobrar hay que usar una PC habilitada como caja.",
        )


def _auditar(
    session: AsyncSession, actor: User, terminal: PosTerminal, tipo: str
) -> None:
    session.add(
        DecisionAuditLog(
            tenant_id=terminal.tenant_id,
            decision_type=tipo,
            decision_data={
                "record_type": "pos_terminal",
                "record_id": str(terminal.id),
                "name": terminal.name,
                "source": "ui",
            },
            triggered_by="ui:pos_terminals",
            actor_user_id=actor.user_id,
            context={"endpoint": "pos.terminals"},
            created_at=datetime.now(UTC),
        )
    )


async def habilitar(
    session: AsyncSession, actor: User, nombre: str
) -> tuple[PosTerminal, str]:
    """Crea la terminal y devuelve el secreto EN CLARO, que no se vuelve a ver."""
    nombre = nombre.strip()
    ocupado = (
        await session.execute(
            select(PosTerminal.id).where(
                PosTerminal.tenant_id == actor.tenant_id,
                PosTerminal.name == nombre,
                PosTerminal.disabled_at.is_(None),
            )
        )
    ).first()
    if ocupado is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "TERMINAL_NAME_TAKEN",
                "message": "Ya hay una caja activa con ese nombre.",
            },
        )
    secreto = generar_secreto()
    terminal = PosTerminal(
        id=uuid.uuid4(),
        tenant_id=actor.tenant_id,
        name=nombre,
        credential_hash=hash_de_secreto(secreto),
        created_by_user_id=actor.user_id,
    )
    session.add(terminal)
    await session.flush()
    _auditar(session, actor, terminal, "POS_TERMINAL_ENROLLED")
    return terminal, secreto


async def listar(session: AsyncSession, tenant_id: uuid.UUID) -> list[PosTerminal]:
    return list(
        (
            await session.execute(
                select(PosTerminal)
                .where(PosTerminal.tenant_id == tenant_id)
                .order_by(PosTerminal.disabled_at.is_not(None), PosTerminal.name)
            )
        )
        .scalars()
        .all()
    )


async def dar_de_baja(
    session: AsyncSession, actor: User, terminal_id: uuid.UUID
) -> PosTerminal:
    """Idempotente: dar de baja una terminal ya dada de baja no cambia nada."""
    terminal = (
        await session.execute(
            select(PosTerminal).where(
                PosTerminal.id == terminal_id, PosTerminal.tenant_id == actor.tenant_id
            )
        )
    ).scalar_one_or_none()
    if terminal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Caja no encontrada.")
    if terminal.disabled_at is None:
        terminal.disabled_at = datetime.now(UTC)
        terminal.disabled_by_user_id = actor.user_id
        _auditar(session, actor, terminal, "POS_TERMINAL_DISABLED")
        await session.flush()
    return terminal
