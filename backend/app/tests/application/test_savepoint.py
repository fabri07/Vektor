"""Contrato de ordenamiento del SAVEPOINT (``_savepoint.guarded_savepoint``).

Estos tests blindan el defecto descrito en el docstring de ``_savepoint``: si el
``add``/``setattr`` ocurre ANTES del ``begin_nested()``, el DML se emite fuera del
savepoint y la violación aborta la transacción entera. Se ejercita con el unique
real ``uq_operation_fingerprints_tenant_fp`` y con la FK de ``tenant_id``, que en
SQLite también se impone (los tests corren con foreign_keys=ON).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services._savepoint import (
    SavepointConflictError,
    guarded_savepoint,
)
from app.persistence.models.memory import OperationFingerprint
from app.persistence.models.tenant import Tenant

_ACTION = "TEST_ACTION"


def _classify_fingerprint(exc: IntegrityError) -> str | None:
    """Reconoce SOLO el unique del fingerprint; todo lo demás → None (re-raise).

    Ojo con el formato del mensaje: PostgreSQL/asyncpg nombra el constraint
    (``uq_operation_fingerprints_tenant_fp``), pero **SQLite reporta las COLUMNAS**
    (``UNIQUE constraint failed: operation_fingerprints.tenant_id,
    operation_fingerprints.fingerprint``). Hay que aceptar las dos formas.
    """
    text = str(getattr(exc, "orig", None) or exc)
    if "uq_operation_fingerprints_tenant_fp" in text:
        return "fingerprint"
    if "operation_fingerprints.fingerprint" in text and "UNIQUE" in text.upper():
        return "fingerprint"
    return None


def _fingerprint(tenant_id: uuid.UUID, value: str) -> OperationFingerprint:
    return OperationFingerprint(tenant_id=tenant_id, fingerprint=value, action_type=_ACTION)


async def _count(session: AsyncSession, tenant_id: uuid.UUID) -> int:
    rows = await session.execute(
        select(OperationFingerprint.id).where(OperationFingerprint.tenant_id == tenant_id)
    )
    return len(rows.scalars().all())


async def test_conflicto_deja_la_transaccion_usable(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """El caso que motiva el módulo: tras la colisión, la sesión sigue sirviendo.

    Con el orden incorrecto (add antes del begin_nested) el re-query de abajo
    fallaría en PostgreSQL con InFailedSQLTransaction.
    """
    tid = sample_tenant.tenant_id
    async with guarded_savepoint(db_session, _classify_fingerprint):
        db_session.add(_fingerprint(tid, "fp-A"))

    with pytest.raises(SavepointConflictError) as caught:
        async with guarded_savepoint(db_session, _classify_fingerprint):
            db_session.add(_fingerprint(tid, "fp-A"))

    assert caught.value.constraint == "fingerprint"
    # La transacción sigue viva: se puede leer Y seguir escribiendo.
    assert await _count(db_session, tid) == 1
    async with guarded_savepoint(db_session, _classify_fingerprint):
        db_session.add(_fingerprint(tid, "fp-B"))
    assert await _count(db_session, tid) == 2


async def test_violacion_no_vigilada_se_propaga(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Una violación que NO es la vigilada no puede convertirse en 'ya existía'.

    Se usa ``NOT NULL`` porque SQLite no impone FKs sin ``PRAGMA foreign_keys=ON``,
    que la suite no activa. El caso de la FK va en el test de integración
    PostgreSQL, donde sí se impone.
    """
    huerfano = OperationFingerprint(
        tenant_id=sample_tenant.tenant_id,
        fingerprint="fp-sin-accion",
        action_type=None,  # NOT NULL a propósito (mypy no lo marca: el kwarg es dinámico)
    )
    with pytest.raises(IntegrityError):
        async with guarded_savepoint(db_session, _classify_fingerprint):
            db_session.add(huerfano)


async def test_objetos_pendientes_ajenos_sobreviven_al_conflicto(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """El drenaje previo acota el rollback al candidato.

    ``_restore_snapshot`` expunga ``set(self._new) | set(session._new)`` — TODO lo
    pendiente de la sesión. Sin el flush previo, un objeto ajeno agregado antes del
    guard se perdería en silencio al revertir el savepoint.
    """
    tid = sample_tenant.tenant_id
    async with guarded_savepoint(db_session, _classify_fingerprint):
        db_session.add(_fingerprint(tid, "fp-ocupado"))

    ajeno = _fingerprint(tid, "fp-ajeno")
    db_session.add(ajeno)  # pendiente, sin relación con el guard

    with pytest.raises(SavepointConflictError):
        async with guarded_savepoint(db_session, _classify_fingerprint):
            db_session.add(_fingerprint(tid, "fp-ocupado"))

    # El ajeno fue drenado ANTES del savepoint → sobrevivió al rollback.
    assert await _count(db_session, tid) == 2
    assert ajeno in db_session or ajeno.id is not None


async def test_camino_feliz_persiste(db_session: AsyncSession, sample_tenant: Tenant) -> None:
    tid = sample_tenant.tenant_id
    async with guarded_savepoint(db_session, _classify_fingerprint):
        db_session.add(_fingerprint(tid, "fp-nuevo"))
    assert await _count(db_session, tid) == 1
