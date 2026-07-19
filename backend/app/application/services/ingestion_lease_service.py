"""Lease per-file del confirm de ingestión (Fase 4 — concurrencia).

El confirm corre el import inline y síncrono. Sin un lease, dos confirms del
mismo archivo (doble-click, dos pestañas, retry impaciente) pasan ambos el
chequeo de estado y corren el import completo en paralelo. Este servicio aporta
la exclusión mutua **per-file** vía un CAS atómico sobre ``uploaded_files``:

- ``acquire_import_lease``  : CAS ``NEEDS_CONFIRMATION → IMPORTING`` (o takeover
  de un ``IMPORTING`` viejo/stale) seteando un token, y **COMMITEA** sobre la
  sesión del request → el ``IMPORTING`` queda visible para un confirm concurrente
  ANTES de que arranque el import largo. El CAS con ``rowcount`` es atómico a
  nivel de fila en Postgres (row lock), así que dos requests concurrentes se
  serializan y sólo uno gana.
- ``finalize_import_lease`` : transición final ``IMPORTING → DONE``
  **token-checked**, SIN commit (la cierra el commit del request, junto con los
  inserts del import). Si el token ya no es el nuestro (nos robaron el lease por
  takeover) → lanza ``ImportLeaseLostError`` → el caller hace rollback → no queda
  dato a medias.
- ``release_import_lease``  : compensación ante error — restaura
  ``NEEDS_CONFIRMATION`` y limpia el lease, sólo si el token sigue siendo
  nuestro, y commitea. El caller DEBE haber hecho ``rollback`` ANTES (para no
  compensar con locks del import todavía tomados).

Todo corre sobre la **sesión del request** (no una sesión aparte): el confirm ya
maneja su propia transacción y el commit intermedio del CAS es lo que da la
visibilidad. Usar la sesión del request también respeta el override de DB de los
tests (``join_transaction_mode="create_savepoint"``).

Ortogonal al lease per-tenant de F3 (``maintenance_lock_service``): el import ya
toma el advisory shared de ese servicio dentro de ``insert_confirmed_data``;
este lease es otra dimensión (per-file, no per-tenant).

**Reloj de PostgreSQL siempre** (``func.now()``), nunca el del proceso Python.
La expiración del takeover usa ``make_interval`` POSICIONAL (los kwargs a
``func.*`` no se traducen a args SQL con nombre en SQLAlchemy 2.0 → revientan;
ver F3-T8).
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from sqlalchemy import ColumnElement, func, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.observability.logger import get_logger
from app.persistence.models.file import (
    PROCESSING_STATUS_DONE,
    PROCESSING_STATUS_IMPORTING,
    PROCESSING_STATUS_NEEDS_CONFIRMATION,
    UploadedFile,
)

logger = get_logger(__name__)

# TTL por defecto (fallback si settings no está disponible). El valor operativo
# real viene de `settings.INGESTION_IMPORT_LEASE_TTL_SECONDS` (configurable). Un
# TTL vencido NO prueba que el proceso murió — también podría robarle el lease a
# un import legítimamente lento; el fencing por token (finalize) garantiza que
# aun con trabajo solapado transitorio, sólo el token vigente puede confirmar.
DEFAULT_IMPORT_LEASE_TTL_SECONDS = 15 * 60


def _resolve_ttl(ttl_seconds: int | None) -> int:
    """TTL efectivo: el explícito, si no el de settings, si no el default."""
    if ttl_seconds is not None:
        return ttl_seconds
    try:
        return get_settings().INGESTION_IMPORT_LEASE_TTL_SECONDS
    except Exception:  # noqa: BLE001 — defensivo, nunca romper el confirm por settings
        return DEFAULT_IMPORT_LEASE_TTL_SECONDS


class ImportLeaseLostError(RuntimeError):
    """El intento perdió el lease (un takeover lo robó con otro token).

    La transición final a ``DONE`` no matcheó (``rowcount == 0``); el caller
    debe hacer rollback de toda la transacción del import.
    """

    def __init__(self, file_id: uuid.UUID) -> None:
        super().__init__(f"import lease lost for file {file_id}")
        self.file_id = file_id


def _stale_before_expr(dialect_name: str, ttl_seconds: int) -> ColumnElement[Any]:
    """Expresión SQL ``now() - ttl_seconds`` en el reloj de la DB, por dialecto.

    Un ``IMPORTING`` con ``import_started_at`` anterior a este umbral se
    considera stale y es candidato a takeover. Postgres soporta ``make_interval``
    (posicional, ver docstring del módulo); SQLite (tests) arma el intervalo con
    ``func.datetime(func.now(), '-N seconds')``.
    """
    if dialect_name == "postgresql":
        # make_interval(years, months, weeks, days, hours, mins, secs) — secs 7º, POSICIONAL.
        return func.now() - func.make_interval(0, 0, 0, 0, 0, 0, ttl_seconds)
    return func.datetime(func.now(), f"-{int(ttl_seconds)} seconds")


async def acquire_import_lease(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    file_id: uuid.UUID,
    token: uuid.UUID,
    ttl_seconds: int | None = None,
) -> bool:
    """Toma el lease del import: CAS ``NEEDS_CONFIRMATION → IMPORTING`` o takeover.

    COMMITEA el ``IMPORTING`` sobre la sesión del request — así un confirm
    concurrente lo ve antes de que arranque el import largo. Devuelve ``True`` si
    tomó el lease; ``False`` si otro intento lo tiene vivo (→ el caller responde
    409). Loguea ``importing_stale_takeover`` cuando roba un lease vencido.

    ``ttl_seconds=None`` → usa ``settings.INGESTION_IMPORT_LEASE_TTL_SECONDS``.
    Nunca toma el lease de un archivo soft-deleted (``deleted_at IS NULL`` en
    ambos pasos): cierra la carrera delete↔confirm (borrar mientras se confirma).
    """
    ttl_seconds = _resolve_ttl(ttl_seconds)
    # Paso 1: CAS normal desde NEEDS_CONFIRMATION (sólo archivos vivos).
    result = await session.execute(
        update(UploadedFile)
        .where(
            UploadedFile.id == file_id,
            UploadedFile.tenant_id == tenant_id,
            UploadedFile.deleted_at.is_(None),
            UploadedFile.processing_status == PROCESSING_STATUS_NEEDS_CONFIRMATION,
        )
        .values(
            processing_status=PROCESSING_STATUS_IMPORTING,
            import_attempt_id=token,
            import_started_at=func.now(),
            import_phase="inserting",
        )
    )
    if cast("CursorResult[Any]", result).rowcount == 1:
        await session.commit()
        return True

    # Paso 2: takeover de un IMPORTING stale (proceso muerto / import colgado).
    # `import_started_at IS NULL` se trata como stale recuperable (estado corrupto:
    # un IMPORTING sin timestamp nunca vencería y quedaría bloqueado para siempre).
    bind = session.get_bind()
    stale_before = _stale_before_expr(bind.dialect.name, ttl_seconds)
    result2 = await session.execute(
        update(UploadedFile)
        .where(
            UploadedFile.id == file_id,
            UploadedFile.tenant_id == tenant_id,
            UploadedFile.deleted_at.is_(None),
            UploadedFile.processing_status == PROCESSING_STATUS_IMPORTING,
            (UploadedFile.import_started_at.is_(None))
            | (UploadedFile.import_started_at < stale_before),
        )
        .values(
            processing_status=PROCESSING_STATUS_IMPORTING,
            import_attempt_id=token,
            import_started_at=func.now(),
            import_phase="inserting",
        )
    )
    if cast("CursorResult[Any]", result2).rowcount == 1:
        await session.commit()
        logger.info(
            "ingestion.confirm.importing_stale_takeover",
            file_id=str(file_id),
            token=str(token),
            ttl_seconds=ttl_seconds,
        )
        return True

    # No se pudo tomar: liberar cualquier lock de los UPDATE fallidos y avisar 409.
    await session.rollback()
    return False


async def finalize_import_lease(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    file_id: uuid.UUID,
    token: uuid.UUID,
    compact_summary: dict[str, Any],
) -> None:
    """Transición final ``IMPORTING → DONE``, token-checked, y limpia el lease.

    SIN commit: corre en la sesión del request y la cierra el commit final del
    request (misma transacción que los inserts del import) — así si el token ya
    no es el nuestro, el rollback del caller descarta también los datos. Lanza
    ``ImportLeaseLostError`` si ``rowcount != 1``.
    """
    result = await session.execute(
        update(UploadedFile)
        .where(
            UploadedFile.id == file_id,
            UploadedFile.tenant_id == tenant_id,
            UploadedFile.processing_status == PROCESSING_STATUS_IMPORTING,
            UploadedFile.import_attempt_id == token,
        )
        .values(
            processing_status=PROCESSING_STATUS_DONE,
            parsed_summary_json=compact_summary,
            import_attempt_id=None,
            import_started_at=None,
            import_phase=None,
        )
    )
    if cast("CursorResult[Any]", result).rowcount != 1:
        raise ImportLeaseLostError(file_id)


async def release_import_lease(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    file_id: uuid.UUID,
    token: uuid.UUID,
) -> None:
    """Compensa un import fallido: restaura ``NEEDS_CONFIRMATION`` + limpia lease.

    Sólo si el ``import_attempt_id`` sigue siendo nuestro token (si un takeover
    ya lo robó, no pisamos el estado del nuevo dueño). Commitea. Best-effort: NO
    re-lanza — no debe tapar la excepción original del caller. El caller DEBE
    haber hecho ``rollback`` de la sesión ANTES de llamar (los locks del import
    ya liberados; la sesión queda limpia para el UPDATE de restauración).
    """
    try:
        await session.execute(
            update(UploadedFile)
            .where(
                UploadedFile.id == file_id,
                UploadedFile.tenant_id == tenant_id,
                UploadedFile.processing_status == PROCESSING_STATUS_IMPORTING,
                UploadedFile.import_attempt_id == token,
            )
            .values(
                processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
                import_attempt_id=None,
                import_started_at=None,
                import_phase=None,
            )
        )
        await session.commit()
    except Exception:  # noqa: BLE001 — best-effort, no debe tapar el error original
        logger.warning(
            "ingestion.confirm.lease_release_failed",
            file_id=str(file_id),
            token=str(token),
        )
