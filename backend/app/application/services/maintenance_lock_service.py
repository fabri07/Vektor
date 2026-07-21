"""Lease por-tenant + advisory locks transaccionales (Fase 3 — dedup de productos).

Un guard solo a nivel FastAPI deja una carrera TOCTOU (un request que ya pasó
el guard escribe mientras el dedup está fusionando) y no cubre ventas
manuales/chat/reread/compra interna. La exclusión mutua REAL es un
**advisory lock de Postgres a nivel transacción**:

- Los *writers* (cualquier escritura de negocio del tenant) toman el lock
  **shared** (``pg_advisory_xact_lock_shared``).
- La reparación (script de dedup) toma el lock **exclusive**
  (``pg_advisory_xact_lock``): espera a que no haya ningún shared en vuelo y
  bloquea nuevos shared mientras corre.
- Ambos se liberan solos al commit/rollback de la transacción actual (por eso
  la variante ``_xact_``) — no hace falta un release explícito.

El lease en tabla (``tenant_maintenance_locks``) es el estado *observable*:
fencing token (``lease_id``), TTL y recuperación ante caída del proceso de
reparación (si el script muere sin liberar, el lease expira solo).

**Reloj de PostgreSQL siempre** (``func.now()``), nunca el del proceso Python
— evita clock-skew entre el proceso que corre el script y la base.

NO cablea los write boundaries en los endpoints/servicios de negocio (eso es
F3-T3) — acá solo viven los primitivos.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from sqlalchemy import ColumnElement, func, select, text
from sqlalchemy.engine import CursorResult, Dialect
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.maintenance_lock import TenantMaintenanceLock


def _advisory_key(tenant_id: uuid.UUID) -> int:
    """Deriva una clave bigint determinística y estable para el advisory lock.

    ``pg_advisory_xact_lock[_shared]`` recibe un ``bigint`` (con signo). Se
    toman los primeros 8 bytes del UUID del tenant como entero big-endian con
    signo — es determinística (mismo tenant → misma clave siempre) y estable
    entre procesos (no depende de estado en memoria).

    CRÍTICO: ``acquire_write_lock_shared`` y ``acquire_maintenance_lock_exclusive``
    DEBEN derivar la clave con esta misma función para el mismo ``tenant_id``.
    Si usaran claves distintas, shared y exclusive tomarían locks de Postgres
    *distintos* y nunca se serializarían entre sí — la exclusión mutua dejaría
    de cumplirse silenciosamente.
    """
    return int.from_bytes(tenant_id.bytes[:8], "big", signed=True)


def _expires_expr(dialect: str, ttl_seconds: int) -> ColumnElement[Any]:
    """Expresión SQL de ``now() + ttl_seconds`` en el reloj de la DB, por dialecto.

    Postgres soporta ``make_interval``; SQLite (tests) no, así que se arma el
    intervalo con ``func.datetime(func.now(), '+N seconds')``. Encapsulado acá
    para que ``acquire`` no tenga que ramificar por dialecto además de en el
    upsert.
    """
    if dialect == "postgresql":
        # make_interval(years, months, weeks, days, hours, mins, secs) — secs es el 7º.
        # Se pasa POSICIONAL a propósito: los kwargs a func.* NO se traducen a args SQL
        # con nombre en SQLAlchemy 2.0 (se interpretan como opciones de Function y
        # revientan con TypeError). Ver test de integración PG del lease (F3-T8).
        return func.now() + func.make_interval(0, 0, 0, 0, 0, 0, ttl_seconds)
    return func.datetime(func.now(), f"+{int(ttl_seconds)} seconds")


async def acquire(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    reason: str,
    ttl_seconds: int,
    created_by: str,
) -> uuid.UUID | None:
    """Adquiere (o roba, si expiró) el lease de mantenimiento del tenant.

    Atómico vía upsert: ``INSERT ... ON CONFLICT (tenant_id) DO UPDATE ...
    WHERE expires_at < now()``. Si el lease vivo es ajeno (no expiró), el
    ``WHERE`` del ``DO UPDATE`` no matchea, Postgres deja la fila intacta y no
    la devuelve por ``RETURNING`` → no hay fila → ``None`` (ya bloqueado, no
    se roba). Si la tabla no tenía fila para el tenant o el lease expiró, se
    inserta/actualiza y se devuelve el ``lease_id`` recién generado.
    """
    lease_id = uuid.uuid4()
    bind = db.get_bind()
    dialect: Dialect = bind.dialect
    dialect_name = dialect.name
    expires_expr = _expires_expr(dialect_name, ttl_seconds)

    values = {
        "id": uuid.uuid4(),  # default=uuid.uuid4 es Python-side; INSERT por Core no lo aplica solo.
        "tenant_id": tenant_id,
        "lease_id": lease_id,
        "reason": reason,
        "created_by": created_by,
        "acquired_at": func.now(),
        "expires_at": expires_expr,
        "heartbeat_at": func.now(),
    }
    set_ = {
        "lease_id": lease_id,
        "reason": reason,
        "created_by": created_by,
        "acquired_at": func.now(),
        "expires_at": expires_expr,
        "heartbeat_at": func.now(),
    }

    if dialect_name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as _pg_insert  # noqa: PLC0415

        stmt = (
            _pg_insert(TenantMaintenanceLock)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["tenant_id"],
                set_=set_,
                where=(TenantMaintenanceLock.expires_at < func.now()),
            )
            .returning(TenantMaintenanceLock.lease_id)
        )
    elif dialect_name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as _sqlite_insert  # noqa: PLC0415

        stmt = (
            _sqlite_insert(TenantMaintenanceLock)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["tenant_id"],
                set_=set_,
                where=(TenantMaintenanceLock.expires_at < func.now()),
            )
            .returning(TenantMaintenanceLock.lease_id)
        )
    else:  # pragma: no cover — solo PG (prod) y SQLite (tests) están soportados
        raise NotImplementedError(f"MaintenanceLockService no soporta el dialecto {dialect_name!r}")

    result = await db.execute(stmt)
    returned_lease_id = result.scalar_one_or_none()
    if returned_lease_id is None:
        return None
    if returned_lease_id != lease_id:  # pragma: no cover — defensivo, no debería ocurrir
        return None
    return lease_id


async def renew(db: AsyncSession, lease_id: uuid.UUID, ttl_seconds: int) -> bool:
    """Renueva el TTL del lease si sigue vivo y es el lease indicado.

    No renueva (y devuelve ``False``) si el ``lease_id`` no existe, es de otro
    lease, o ya expiró — en ese caso el llamador perdió el lease y no debe
    seguir asumiendo exclusión.
    """
    bind = db.get_bind()
    dialect_name = bind.dialect.name
    expires_expr = _expires_expr(dialect_name, ttl_seconds)

    result = await db.execute(
        select(TenantMaintenanceLock.id)
        .where(
            TenantMaintenanceLock.lease_id == lease_id,
            TenantMaintenanceLock.expires_at >= func.now(),
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        return False

    from sqlalchemy import update as _update  # noqa: PLC0415

    result2 = await db.execute(
        _update(TenantMaintenanceLock)
        .where(
            TenantMaintenanceLock.lease_id == lease_id,
            TenantMaintenanceLock.expires_at >= func.now(),
        )
        .values(expires_at=expires_expr, heartbeat_at=func.now())
    )
    return cast("CursorResult[Any]", result2).rowcount > 0


async def release(db: AsyncSession, lease_id: uuid.UUID) -> bool:
    """Libera (borra) el lease. ``False`` si el ``lease_id`` no existe."""
    from sqlalchemy import delete as _delete  # noqa: PLC0415

    result = await db.execute(
        _delete(TenantMaintenanceLock).where(TenantMaintenanceLock.lease_id == lease_id)
    )
    return cast("CursorResult[Any]", result).rowcount > 0


async def is_locked(db: AsyncSession, tenant_id: uuid.UUID) -> bool:
    """``True`` si el tenant tiene un lease vivo (no expirado) ahora mismo."""
    result = await db.execute(
        select(text("1"))
        .select_from(TenantMaintenanceLock)
        .where(
            TenantMaintenanceLock.tenant_id == tenant_id,
            TenantMaintenanceLock.expires_at > func.now(),
        )
    )
    return result.scalar_one_or_none() is not None


async def acquire_write_lock_shared(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Toma el advisory lock SHARED del tenant (exclusión mutua de writers).

    Se libera solo al commit/rollback de la transacción actual
    (``pg_advisory_xact_lock_shared``). En SQLite es un no-op documentado: el
    lock real solo existe en Postgres; los tests SQLite son single-thread y no
    necesitan (ni pueden) exclusión mutua real.
    """
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        return
    key = _advisory_key(tenant_id)
    await db.execute(text("SELECT pg_advisory_xact_lock_shared(:key)"), {"key": key})


async def acquire_maintenance_lock_exclusive(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Toma el advisory lock EXCLUSIVE del tenant (barrera de reparación).

    Espera a que no haya ningún SHARED en vuelo y bloquea nuevos SHARED
    mientras esté tomado. Se libera solo al commit/rollback
    (``pg_advisory_xact_lock``). En SQLite es un no-op documentado (ver
    ``acquire_write_lock_shared``).
    """
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        return
    key = _advisory_key(tenant_id)
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
