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

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services._savepoint import (
    SavepointConflictError,
    guarded_savepoint,
    unique_violation_classifier,
)
from app.persistence.models.idempotency import IdempotencyRecord
from app.persistence.models.memory import OperationFingerprint

# Prefijo de namespace para no colisionar con los fingerprints SHA-256 del agente.
_IDEMPOTENCY_PREFIX = "idem:"

_FINGERPRINT_CONFLICT = unique_violation_classifier(
    "fingerprint",
    constraint="uq_operation_fingerprints_tenant_fp",
    columns=("operation_fingerprints.tenant_id", "operation_fingerprints.fingerprint"),
)


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
    # `guarded_savepoint` aporta el ordenamiento (drenar fuera del try, agregar dentro
    # del savepoint) y, sobre todo, el clasificador: un `except IntegrityError` a secas
    # reportaría una FK rota o un NOT NULL del propio fingerprint como "duplicado",
    # devolviendo False y haciendo que el caller responda 409 por la razón equivocada.
    try:
        async with guarded_savepoint(session, _FINGERPRINT_CONFLICT):
            session.add(
                OperationFingerprint(
                    tenant_id=tenant_id,
                    fingerprint=fingerprint,
                    action_type=action_type,
                )
            )
    except SavepointConflictError:
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


async def legacy_idempotency_key_claimed(
    session: AsyncSession, tenant_id: uuid.UUID, key: str
) -> bool:
    """¿Esta clave ya se reclamó con el mecanismo VIEJO (`operation_fingerprints`)?

    Existe por el deploy de B2: `manual-batch` pasó de `claim_idempotency_key` a
    `claim_idempotent_request`, que mira OTRA tabla. Una venta encolada offline
    antes del deploy, que el servidor guardó pero cuya respuesta se perdió, se
    reintenta después con la misma clave; sin esta consulta la tabla nueva no la
    conoce y la venta se crea DOS veces, con el stock descontado dos veces. Sólo
    lectura: no reclama nada.
    """
    fila = await session.execute(
        select(OperationFingerprint.id).where(
            OperationFingerprint.tenant_id == tenant_id,
            OperationFingerprint.fingerprint == _idempotency_fingerprint(key),
        )
    )
    return fila.first() is not None


# ═══════════════════════════════════════════════════════════════════════════════
# Idempotencia RECUPERABLE (bloque B2 del POS)
# ═══════════════════════════════════════════════════════════════════════════════
#
# `claim_idempotency_key` evita el duplicado y nada más: devuelve un bool y el
# caller contesta un 409 vacío. Para una caja falta lo otro — "el servidor guardó
# la venta y nunca recibí la respuesta" — que con un 409 sin cuerpo deja al cajero
# sin saber si cobró. Y falta distinguir el reintento legítimo (misma clave, mismo
# contenido) de una clave reusada por error (misma clave, contenido distinto), que
# hoy se traga en silencio y hace desaparecer la segunda operación.
#
# Las dos funciones de abajo conviven con la vieja: las 10 rutas que no son de caja
# siguen igual. Migrar una ruta es cambiar su claim y agregarle el registro del
# resultado antes del return.

_IDEMPOTENCY_RECORD_CONFLICT = unique_violation_classifier(
    "idempotency_key",
    constraint="uq_idempotency_records_tenant_key",
    columns=("idempotency_records.tenant_id", "idempotency_records.key"),
)


def hash_peticion(payload: BaseModel | Mapping[str, Any]) -> str:
    """Huella del contenido COMERCIAL de un pedido ya validado.

    Sobre el modelo validado y no sobre los bytes crudos, por dos razones: el
    orden de las claves del JSON y los espacios no cambian lo que se pide (y
    producirían un conflicto falso, que rechaza un reintento legítimo), y el
    modelo ya normalizó defaults y fechas, así que dos formas de escribir lo
    mismo dan la misma huella.
    """
    # `exclude_defaults`: un campo que el cliente no mandó y vale su default no
    # entra a la huella. Sin esto, agregar en un deploy un campo con default
    # cambiaba el hash de los reintentos YA encolados offline, que recibían 409
    # IDEMPOTENCY_KEY_REUSED por una venta que sí existe. Mandar el default
    # explícito o no mandarlo describen el mismo pedido, así que es correcto que
    # den la misma huella.
    datos = (
        payload.model_dump(mode="json", exclude_defaults=True)
        if isinstance(payload, BaseModel)
        else dict(payload)
    )
    canonico = json.dumps(datos, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonico.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Reclamada:
    """La clave es nueva: el caller tiene que ejecutar la operación."""


@dataclass(frozen=True)
class Repeticion:
    """Ya se ejecutó con ESTE mismo contenido.

    ``respuesta`` es ``None`` cuando la ejecución original no registró su
    resultado (una ruta que reclama y no registra). El caller degrada al 409 de
    siempre: no poder devolver el original es distinto de poder inventarlo.
    """

    respuesta: dict[str, Any] | None
    http_status: int


@dataclass(frozen=True)
class ClaveReusada:
    """Misma clave, contenido distinto. No es un reintento: es un error del cliente."""

    action_type: str


async def idempotent_request_exists(
    session: AsyncSession, tenant_id: uuid.UUID, key: str
) -> bool:
    """¿Esa clave ya tiene un registro? Sólo lectura: NO reclama nada.

    Sirve para autorizar distinto crear y recuperar (B5): una operación que ya
    existe se devuelve aunque al cajero le hayan revocado el permiso después, y
    el chequeo de permisos de una NUEVA va antes del claim, así que un 403 no
    consume la clave. Una carrera entre esta lectura y el claim no rompe nada:
    el claim sigue siendo el candado y contesta `Repeticion` igual.
    """
    fila = await session.execute(
        select(IdempotencyRecord.id).where(
            IdempotencyRecord.tenant_id == tenant_id,
            IdempotencyRecord.key == key,
        )
    )
    return fila.first() is not None


async def claim_idempotent_request(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    key: str,
    action_type: str,
    payload: BaseModel | Mapping[str, Any],
) -> Reclamada | Repeticion | ClaveReusada:
    """Reclama una ``Idempotency-Key`` guardando de qué era.

    Race-safe por el UNIQUE ``(tenant_id, key)``: dos peticiones simultáneas con
    la misma clave no pueden reclamar las dos — la segunda espera en el índice y,
    cuando la primera commitea, entra por la rama de repetición.
    """
    huella = hash_peticion(payload)
    try:
        async with guarded_savepoint(session, _IDEMPOTENCY_RECORD_CONFLICT):
            session.add(
                IdempotencyRecord(
                    tenant_id=tenant_id,
                    key=key,
                    action_type=action_type,
                    request_hash=huella,
                )
            )
    except SavepointConflictError:
        existente = (
            await session.execute(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.tenant_id == tenant_id,
                    IdempotencyRecord.key == key,
                )
            )
        ).scalar_one()
        if existente.request_hash != huella:
            return ClaveReusada(action_type=existente.action_type)
        return Repeticion(
            respuesta=dict(existente.response_json) if existente.response_json else None,
            http_status=existente.http_status,
        )
    return Reclamada()


async def record_idempotent_response(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    key: str,
    respuesta: BaseModel | Mapping[str, Any],
    http_status: int = 201,
) -> None:
    """Guarda el resultado en la MISMA transacción que la entidad que lo produjo.

    Que commiteen juntos es lo que hace confiable la recuperación: si se guardara
    después, una caída en el medio dejaría una venta persistida cuyo reintento no
    puede recuperar nada — el mismo agujero, corrido un paso.
    """
    datos = (
        respuesta.model_dump(mode="json")
        if isinstance(respuesta, BaseModel)
        else dict(respuesta)
    )
    await session.execute(
        update(IdempotencyRecord)
        .where(
            IdempotencyRecord.tenant_id == tenant_id,
            IdempotencyRecord.key == key,
        )
        .values(response_json=datos, http_status=http_status)
    )
