"""La relectura y el borrado del mismo archivo, a la vez (I3-1 caso C, I4-1).

Los dos órdenes, contra Postgres real: SQLite no tiene advisory locks y la
suite pasaría sin probar nada (``[[feedback_sqlite_masks_postgres]]``).

- **Borrar durante una relectura en curso:** el borrado pide el candado de
  importación antes de marcar el archivo, así que espera a que la relectura
  commitee y después revierte también lo que ella cargó. Antes de T2-b no se
  excluían y lo que la relectura commiteaba después del borrado sobrevivía.
- **Releer durante un borrado en curso:** la relectura toma el mismo candado y
  recién después mira el archivo: espera al borrado, lo ve borrado y no
  reimporta nada. Sin esa lectura bajo el candado, reimportaba el archivo
  entero sobre lo que el borrado acababa de revertir.

Los caminos sin concurrencia (HTTP, worker, deshacer) están en
``api/v1/test_relectura_archivo_borrado.py``::

    TEST_PG_DSN='postgresql+asyncpg://vektor:vektor@localhost:5432/vektor_pgtest' \\
        pytest app/tests/integration/test_relectura_archivo_borrado_pg.py -m postgres -n 0 --no-cov
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.application.services import maintenance_lock_service, reread_service, respaldo_service
from app.application.services.file_deletion_service import revert_file_data
from app.persistence.models.file import UploadedFile
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.repair import DataRepairRun
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry
from app.tests.integration import _reread_scenario as esc

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]

_DDL_ADVISORY_KEY = 0x5645_4B54_4F52_1E4B  # "VEKTOR" + I-B

_FILAS = [
    esc.fila(5, esc.PRODUCTO, "2", "2400"),
    esc.fila(6, "Vela vainilla 200g", "3", "3600"),
]
_RELEIDO = [
    esc.fila(5, esc.PRODUCTO, "4", "4800"),
    esc.fila(6, "Vela vainilla 200g", "3", "3600"),
]


@pytest_asyncio.fixture(scope="module")
async def pg_engine() -> AsyncGenerator[AsyncEngine, None]:
    assert TEST_PG_DSN is not None
    engine = await esc.crear_engine(TEST_PG_DSN, _DDL_ADVISORY_KEY)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def sm(pg_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # `autoflush=False` como producción.
    return async_sessionmaker(pg_engine, expire_on_commit=False, autoflush=False)


@pytest_asyncio.fixture
async def archivo(
    sm: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[tuple[uuid.UUID, uuid.UUID], None]:
    """``(tenant, archivo)`` con el archivo de compras ya importado."""
    tid = uuid.uuid4()
    fid = await esc.importar_la_primera_vez(sm, tid, _FILAS)
    try:
        yield tid, fid
    finally:
        async with sm() as s:
            # Todo lo del negocio cuelga del tenant con ON DELETE CASCADE.
            await s.execute(delete(Tenant).where(Tenant.tenant_id == tid))
            await s.commit()


async def _vivos(sm: async_sessionmaker[AsyncSession], fid: uuid.UUID) -> tuple[int, int]:
    """``(gastos, movimientos de stock)`` vivos del archivo, desde una conexión nueva."""
    async with sm() as s:
        gastos = await s.scalar(
            select(func.count())
            .select_from(ExpenseEntry)
            .where(ExpenseEntry.source_upload_id == fid, ExpenseEntry.voided_at.is_(None))
        )
        movimientos = await s.scalar(
            select(func.count())
            .select_from(InventoryMovement)
            .where(
                InventoryMovement.source_upload_id == fid,
                InventoryMovement.voided_at.is_(None),
            )
        )
        return int(gastos or 0), int(movimientos or 0)


async def _empezar_borrado(s: AsyncSession, tid: uuid.UUID, fid: uuid.UUID) -> None:
    """Lo que hace ``DELETE /ingestion/files/{id}``: candados, marca y reversa."""
    await maintenance_lock_service.acquire_write_lock_shared(s, tid)
    await maintenance_lock_service.acquire_import_lock(s, tid)
    await s.execute(
        update(UploadedFile)
        .where(UploadedFile.id == fid, UploadedFile.deleted_at.is_(None))
        .values(deleted_at=func.now())
    )
    await revert_file_data(s, fid, tid)


async def _espera_un_candado(
    sm: async_sessionmaker[AsyncSession], tarea: asyncio.Task[Any], que: str
) -> None:
    """Hasta que la operación quede ESPERANDO un candado en Postgres (mismo
    criterio que ``test_stock_candado_t2b_pg.py``: un sleep fijo no prueba nada)."""
    async with sm() as s:
        for _ in range(200):
            if tarea.done():
                tarea.result()
                raise AssertionError(f"{que} no esperó: terminó antes que la otra")
            esperando = await s.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = "
                    "current_database() AND wait_event_type = 'Lock' "
                    "AND pid <> pg_backend_pid()"
                )
            )
            if esperando:
                return
            await asyncio.sleep(0.05)
    raise AssertionError(f"{que} nunca quedó esperando un candado")


async def test_borrar_durante_una_relectura_en_curso_revierte_tambien_lo_que_ella_cargo(
    sm: async_sessionmaker[AsyncSession],
    archivo: tuple[uuid.UUID, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid, fid = archivo
    llego, seguir = asyncio.Event(), asyncio.Event()
    original = respaldo_service.productos_con_fuente_en_archivo

    async def _pausa(*a: Any, **kw: Any) -> Any:
        # Ya con los candados de la relectura tomados.
        llego.set()
        await seguir.wait()
        return await original(*a, **kw)

    monkeypatch.setattr(reread_service, "productos_con_fuente_en_archivo", _pausa)

    async def _releer() -> uuid.UUID:
        async with sm() as s:
            r = await reread_service.apply_reread(s, fid, tid, fresh_override=esc.summary(_RELEIDO))
            await s.commit()
            return r.run_id

    relectura = asyncio.create_task(_releer())
    await asyncio.wait_for(llego.wait(), 30)

    async def _borrar() -> None:
        async with sm() as s:
            await _empezar_borrado(s, tid, fid)
            await s.commit()

    borrado = asyncio.create_task(_borrar())
    await _espera_un_candado(sm, borrado, "el borrado")
    seguir.set()
    run_id = await asyncio.wait_for(relectura, 60)
    await asyncio.wait_for(borrado, 60)

    assert await _vivos(sm, fid) == (0, 0), "lo que cargó la relectura sobrevivió al borrado"
    async with sm() as s:
        f = await s.get(UploadedFile, fid)
        assert f is not None and f.deleted_at is not None
        run = await s.get(DataRepairRun, run_id)
        assert run is not None and run.status == "APPLIED"
    # Y la relectura aplicada ya no se puede deshacer sobre el archivo borrado.
    async with sm() as s:
        with pytest.raises(reread_service.RereadFileDeletedError):
            await reread_service.undo_reread(s, run_id, tid)
        await s.rollback()
    assert await _vivos(sm, fid) == (0, 0)


async def test_releer_durante_un_borrado_en_curso_espera_y_no_reimporta_nada(
    sm: async_sessionmaker[AsyncSession], archivo: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tid, fid = archivo
    assert await _vivos(sm, fid) == (2, 2)
    borrado = sm()
    await _empezar_borrado(borrado, tid, fid)  # sin commit: retiene el candado

    async def _releer() -> None:
        async with sm() as s:
            await reread_service.apply_reread(s, fid, tid, fresh_override=esc.summary(_RELEIDO))
            await s.commit()

    relectura = asyncio.create_task(_releer())
    await _espera_un_candado(sm, relectura, "la relectura")
    await borrado.commit()
    await borrado.close()

    with pytest.raises(reread_service.RereadFileDeletedError):
        await asyncio.wait_for(relectura, 60)
    assert await _vivos(sm, fid) == (0, 0), "la relectura reimportó el archivo borrado"
