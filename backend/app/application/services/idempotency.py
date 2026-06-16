"""Idempotencia opcional para endpoints POST de creación.

Reusa la tabla `operation_fingerprints` (la misma del dedup del agente) con un
namespace propio (`idem:<key>`) para no chocar con los SHA-256 que registra el
agente.

Estrategia de atomicidad
------------------------
El claim y la creación de la entidad comparten la sesión/transacción de la
request (`get_db_session` hace UN commit al final, o rollback total ante
excepción). El claim se hace con `begin_nested()` (SAVEPOINT) para que un
`IntegrityError` del unique `(tenant_id, fingerprint)` no aborte la transacción
padre — el savepoint se revierte y la transacción exterior sigue viva para poder
responder 409. Si el claim entra pero la creación posterior falla, el rollback
de la dependency revierte también el claim, de modo que un reintento futuro con
la misma key pueda volver a reclamarla.
"""

from __future__ import annotations

import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.memory import OperationFingerprint

# Prefijo de namespace para no colisionar con los fingerprints SHA-256 del agente.
_IDEMPOTENCY_PREFIX = "idem:"


def _idempotency_fingerprint(key: str) -> str:
    return f"{_IDEMPOTENCY_PREFIX}{key}"


async def claim_operation_fingerprint(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    fingerprint: str,
    action_type: str,
) -> bool:
    """Inserta un fingerprint race-safe usando el unique `(tenant_id, fingerprint)`.

    Usa `begin_nested()` (SAVEPOINT) para que el `IntegrityError` del unique no
    aborte la transacción padre.

    Returns:
        True  si insertó el fingerprint (era nuevo).
        False si el fingerprint ya existía (duplicado).
    """
    try:
        async with session.begin_nested():
            session.add(
                OperationFingerprint(
                    tenant_id=tenant_id,
                    fingerprint=fingerprint,
                    action_type=action_type,
                )
            )
    except IntegrityError:
        return False
    return True


async def claim_idempotency_key(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    key: str,
    action_type: str,
) -> bool:
    """Intenta reclamar una `Idempotency-Key` para el tenant.

    Inserta una fila en `operation_fingerprints` con
    `fingerprint = "idem:<key>"`. Race-safe: confía en el unique constraint
    `(tenant_id, fingerprint)` en vez de un SELECT previo.

    Returns:
        True  si reclamó la key (es nueva) → el caller debe crear la entidad.
        False si la key ya existía (replay) → el caller debe responder 409.
    """
    return await claim_operation_fingerprint(
        session, tenant_id, _idempotency_fingerprint(key), action_type
    )
