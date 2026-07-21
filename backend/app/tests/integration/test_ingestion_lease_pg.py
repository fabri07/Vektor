"""Integración PostgreSQL del lease per-file del confirm de ingestión (F4).

El CAS del lease (``UPDATE ... WHERE processing_status='NEEDS_CONFIRMATION'`` con
``rowcount``) sólo se serializa de verdad bajo el row-lock de Postgres. En SQLite
(la suite normal) NO hay concurrencia real: dos requests no pueden correr a la
vez, así que la garantía central de F4 —que dos confirms del mismo archivo no
corran en paralelo— NO se ejercita en la CI SQLite. Este módulo la prueba contra
un Postgres real. Ver ``[[feedback_sqlite_masks_postgres]]``.

Gating: se **skippea limpio** sin ``TEST_PG_DSN``. Para correrlo::

    TEST_PG_DSN='postgresql+asyncpg://vektor:vektor@localhost:5432/vektor' \\
        pytest app/tests/integration/test_ingestion_lease_pg.py -v --no-cov

Aislamiento bajo xdist: cada test usa ``tenant_id``/``file_id`` únicos y la
limpieza borra SOLO esas filas. El schema se crea con ``create_all(checkfirst)``
serializado por un advisory lock para tolerar workers concurrentes.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
import pytest_asyncio
from sqlalchemy import Table, delete, func, select, update
from sqlalchemy.engine import CursorResult  # noqa: F401 — usado por cast("CursorResult[Any]", ...)
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.application.services.ingestion_lease_service import (
    ImportLeaseLostError,
    acquire_import_lease,
    finalize_import_lease,
)
from app.persistence.db.base import Base
from app.persistence.models.file import (
    PROCESSING_STATUS_DONE,
    PROCESSING_STATUS_IMPORTING,
    PROCESSING_STATUS_NEEDS_CONFIRMATION,
    UploadedFile,
)
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]

# Clave arbitraria y estable para serializar el CREATE entre workers xdist.
_DDL_ADVISORY_KEY = 0x5645_4B54_4F52_0F40  # "VEKTOR" + F4


@pytest_asyncio.fixture(scope="module")
async def pg_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Engine asyncpg propio (NullPool → cada sesión, su conexión física).

    Crea SOLO ``tenants`` + ``uploaded_files`` (checkfirst, idempotente). No usa
    ``create_all`` del schema completo: algunos modelos tienen server_defaults que
    sólo son válidos vía la migración Alembic. En CI, ``alembic upgrade head`` crea
    el schema real ANTES (probando la migración) y este checkfirst queda no-op.
    """
    assert TEST_PG_DSN is not None
    engine = create_async_engine(TEST_PG_DSN, poolclass=NullPool)
    # `__table__` está declarado como FromClause; `create_all(tables=...)` espera Table.
    tables: list[Table] = [
        cast("Table", Tenant.__table__),
        cast("Table", User.__table__),
        cast("Table", UploadedFile.__table__),
    ]
    async with engine.begin() as conn:
        from sqlalchemy import text  # noqa: PLC0415

        await conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _DDL_ADVISORY_KEY})
        await conn.run_sync(Base.metadata.create_all, tables=tables, checkfirst=True)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def tenant_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest_asyncio.fixture
async def sessionmaker(
    pg_engine: AsyncEngine, tenant_id: uuid.UUID
) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    sm = async_sessionmaker(pg_engine, expire_on_commit=False)
    try:
        yield sm
    finally:
        async with sm() as s:
            await s.execute(delete(UploadedFile).where(UploadedFile.tenant_id == tenant_id))
            await s.execute(delete(Tenant).where(Tenant.tenant_id == tenant_id))
            await s.commit()


async def _seed_file(
    sm: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    *,
    status: str = PROCESSING_STATUS_NEEDS_CONFIRMATION,
    import_started_at: datetime | None = None,
    import_attempt_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Crea (commit) un tenant + un uploaded_file y devuelve el file_id."""
    file_id = uuid.uuid4()
    async with sm() as s:
        # Flush del tenant ANTES del archivo: sin relationship() la unit-of-work no
        # ordena sola el INSERT del padre → FK violation si van juntos.
        s.add(Tenant(tenant_id=tenant_id, legal_name="T", display_name="T"))
        await s.flush()
        s.add(
            UploadedFile(
                id=file_id,
                tenant_id=tenant_id,
                original_filename="f.xlsx",
                s3_key="k",
                content_type="application/vnd.ms-excel",
                size_bytes=1,
                purpose="ventas",
                processing_status=status,
                import_started_at=import_started_at,
                import_attempt_id=import_attempt_id,
                import_phase="inserting" if status == PROCESSING_STATUS_IMPORTING else None,
            )
        )
        await s.commit()
    return file_id


async def _acquire_in_own_session(
    sm: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    file_id: uuid.UUID,
    token: uuid.UUID,
) -> bool:
    async with sm() as s:
        return await acquire_import_lease(s, tenant_id, file_id, token)


async def test_confirm_concurrente_bloquea_deterministicamente(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """El 2º acquire BLOQUEA en el row-lock del 1º hasta que commitea, y pierde.

    Determinístico (sin race de timing): el holder toma el row-lock con el CAS y
    NO commitea; el competidor corre en task separada y debe bloquear (se afirma
    con ``wait_for`` → ``TimeoutError``, protegido por ``shield`` para que el
    timeout no cancele la task ni corrompa su conexión). Tras el commit del
    holder, el competidor completa y devuelve ``False`` (ve IMPORTING fresco → no
    hay takeover). Prueba la garantía central de F4 que SQLite no puede ejercitar.
    """
    file_id = await _seed_file(sessionmaker, tenant_id)
    token_a, token_b = uuid.uuid4(), uuid.uuid4()

    async with sessionmaker() as holder:
        # Holder toma el row-lock con el CAS (rowcount 1) pero NO commitea.
        res = await holder.execute(
            update(UploadedFile)
            .where(
                UploadedFile.id == file_id,
                UploadedFile.tenant_id == tenant_id,
                UploadedFile.deleted_at.is_(None),
                UploadedFile.processing_status == PROCESSING_STATUS_NEEDS_CONFIRMATION,
            )
            .values(
                processing_status=PROCESSING_STATUS_IMPORTING,
                import_attempt_id=token_a,
                import_started_at=func.now(),
                import_phase="inserting",
            )
        )
        assert cast("CursorResult[Any]", res).rowcount == 1

        # Competidor: acquire en task separada → DEBE bloquear en el row-lock.
        task = asyncio.create_task(
            _acquire_in_own_session(sessionmaker, tenant_id, file_id, token_b)
        )
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
        assert not task.done(), "el 2º acquire NO debería completar mientras el 1º sostiene el lock"

        # Holder libera (commit) → IMPORTING visible → el competidor completa y pierde.
        await holder.commit()
        got = await asyncio.wait_for(task, timeout=10.0)

    assert got is False, "el 2º acquire debe perder (ve IMPORTING fresco, sin takeover)"

    async with sessionmaker() as s:
        row = (
            await s.execute(select(UploadedFile).where(UploadedFile.id == file_id))
        ).scalar_one()
    assert row.processing_status == PROCESSING_STATUS_IMPORTING
    assert row.import_attempt_id == token_a


async def test_acquire_no_toma_lease_de_archivo_borrado(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """Un archivo soft-deleted NO es confirmable: acquire → False (carrera delete↔confirm)."""
    file_id = await _seed_file(sessionmaker, tenant_id)
    async with sessionmaker() as s:
        await s.execute(
            update(UploadedFile)
            .where(UploadedFile.id == file_id)
            .values(deleted_at=func.now())
        )
        await s.commit()

    async with sessionmaker() as s:
        assert await acquire_import_lease(s, tenant_id, file_id, uuid.uuid4()) is False


async def test_release_restaura_needs_confirmation_en_pg(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """Compensación real sobre Postgres: release restaura NEEDS_CONFIRMATION + limpia lease."""
    file_id = await _seed_file(sessionmaker, tenant_id)
    token = uuid.uuid4()
    async with sessionmaker() as s:
        assert await acquire_import_lease(s, tenant_id, file_id, token) is True

    from app.application.services.ingestion_lease_service import (  # noqa: PLC0415
        release_import_lease,
    )

    async with sessionmaker() as s:
        await s.rollback()  # el caller siempre rollbackea el import antes de compensar
        await release_import_lease(s, tenant_id, file_id, token)

    async with sessionmaker() as s:
        row = (
            await s.execute(select(UploadedFile).where(UploadedFile.id == file_id))
        ).scalar_one()
    assert row.processing_status == PROCESSING_STATUS_NEEDS_CONFIRMATION
    assert row.import_attempt_id is None
    assert row.import_started_at is None


async def test_takeover_con_dueno_vivo_el_viejo_no_puede_finalizar(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """Takeover con el dueño anterior AÚN VIVO: sólo el token vigente confirma.

    El viejo, al intentar finalizar con su token, encuentra rowcount 0 →
    ImportLeaseLostError → su import se descarta (rollback), sin doble DONE.
    """
    old_token = uuid.uuid4()
    file_id = await _seed_file(
        sessionmaker,
        tenant_id,
        status=PROCESSING_STATUS_IMPORTING,
        import_started_at=datetime.now(UTC) - timedelta(hours=1),  # stale
        import_attempt_id=old_token,
    )
    new_token = uuid.uuid4()
    async with sessionmaker() as s:
        assert await acquire_import_lease(s, tenant_id, file_id, new_token) is True

    # El dueño viejo (todavía vivo) intenta finalizar con su token → pierde.
    async with sessionmaker() as s:
        with pytest.raises(ImportLeaseLostError):
            await finalize_import_lease(s, tenant_id, file_id, old_token, {"x": 1})
        await s.rollback()

    async with sessionmaker() as s:
        row = (
            await s.execute(select(UploadedFile).where(UploadedFile.id == file_id))
        ).scalar_one()
    # El archivo sigue IMPORTING con el token del nuevo dueño (no pasó a DONE).
    assert row.processing_status == PROCESSING_STATUS_IMPORTING
    assert row.import_attempt_id == new_token


async def test_takeover_recupera_importing_sin_timestamp(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """Un IMPORTING corrupto (import_started_at NULL) es recuperable por takeover."""
    file_id = await _seed_file(
        sessionmaker,
        tenant_id,
        status=PROCESSING_STATUS_IMPORTING,
        import_started_at=None,
        import_attempt_id=uuid.uuid4(),
    )
    new_token = uuid.uuid4()
    async with sessionmaker() as s:
        assert await acquire_import_lease(s, tenant_id, file_id, new_token) is True

    async with sessionmaker() as s:
        row = (
            await s.execute(select(UploadedFile).where(UploadedFile.id == file_id))
        ).scalar_one()
    assert row.import_attempt_id == new_token


async def test_finalize_con_token_ajeno_lanza_y_no_marca_done(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """finalize con un token distinto → ImportLeaseLostError; el archivo NO pasa a DONE."""
    file_id = await _seed_file(sessionmaker, tenant_id)
    token = uuid.uuid4()
    async with sessionmaker() as s:
        assert await acquire_import_lease(s, tenant_id, file_id, token) is True

    async with sessionmaker() as s:
        with pytest.raises(ImportLeaseLostError):
            await finalize_import_lease(s, tenant_id, file_id, uuid.uuid4(), {"x": 1})
        await s.rollback()

    async with sessionmaker() as s:
        row = (
            await s.execute(select(UploadedFile).where(UploadedFile.id == file_id))
        ).scalar_one()
    assert row.processing_status == PROCESSING_STATUS_IMPORTING


async def test_finalize_con_token_propio_marca_done_y_limpia_lease(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    file_id = await _seed_file(sessionmaker, tenant_id)
    token = uuid.uuid4()
    async with sessionmaker() as s:
        assert await acquire_import_lease(s, tenant_id, file_id, token) is True

    async with sessionmaker() as s:
        await finalize_import_lease(s, tenant_id, file_id, token, {"imported_counts": {}})
        await s.commit()

    async with sessionmaker() as s:
        row = (
            await s.execute(select(UploadedFile).where(UploadedFile.id == file_id))
        ).scalar_one()
    assert row.processing_status == PROCESSING_STATUS_DONE
    assert row.import_attempt_id is None
    assert row.import_started_at is None
    assert row.import_phase is None


async def test_takeover_de_importing_stale(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """Un IMPORTING con import_started_at viejo es retomable con un token nuevo."""
    stale = datetime.now(UTC) - timedelta(hours=1)
    file_id = await _seed_file(
        sessionmaker,
        tenant_id,
        status=PROCESSING_STATUS_IMPORTING,
        import_started_at=stale,
        import_attempt_id=uuid.uuid4(),
    )
    new_token = uuid.uuid4()
    async with sessionmaker() as s:
        assert await acquire_import_lease(s, tenant_id, file_id, new_token) is True

    async with sessionmaker() as s:
        row = (
            await s.execute(select(UploadedFile).where(UploadedFile.id == file_id))
        ).scalar_one()
    assert row.import_attempt_id == new_token


async def test_no_takeover_de_importing_fresco(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """Un IMPORTING con import_started_at reciente NO es retomable → 409 (False)."""
    fresh = datetime.now(UTC)
    held_token = uuid.uuid4()
    file_id = await _seed_file(
        sessionmaker,
        tenant_id,
        status=PROCESSING_STATUS_IMPORTING,
        import_started_at=fresh,
        import_attempt_id=held_token,
    )
    async with sessionmaker() as s:
        assert await acquire_import_lease(s, tenant_id, file_id, uuid.uuid4()) is False

    async with sessionmaker() as s:
        row = (
            await s.execute(select(UploadedFile).where(UploadedFile.id == file_id))
        ).scalar_one()
    # El lease del dueño original quedó intacto.
    assert row.import_attempt_id == held_token
