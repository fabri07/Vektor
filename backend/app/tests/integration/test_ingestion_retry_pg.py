"""E3 (H06) — un reintento tiene que poder volver a tomar el archivo.

Es la parte del retry que no se ve mirando `self.retry()`: el claim de E1 sólo
toma archivos en **PENDING**, así que un segundo intento encontraría el archivo
en PROCESSING —el estado en el que lo dejó el intento que falló— y saldría sin
hacer nada. Encender los reintentos sin liberar el claim habría dado tres
intentos que no procesan nada y un archivo trabado.

Por eso el camino de error, ante un error transitorio, **libera** (vuelve a
PENDING, limpia el token) y NO marca FAILED: marcarlo sería mentir sobre un
archivo que todavía tiene intentos, y encima lo dejaría en un estado que el claim
no puede tomar.

Va contra PostgreSQL real porque lo que se afirma es el resultado de `UPDATE`s
condicionales con `rowcount` sobre la misma fila — la liberación lleva el mismo
fencing que el resto del ciclo.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any, cast

import pytest
import pytest_asyncio
from botocore.exceptions import ClientError
from sqlalchemy import Table, delete, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.jobs import ingestion_worker as worker
from app.persistence.db.base import Base
from app.persistence.models.file import (
    PROCESSING_STATUS_FAILED,
    PROCESSING_STATUS_PENDING,
    PROCESSING_STATUS_PROCESSING,
    UploadedFile,
)
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]

_DDL_ADVISORY_KEY = 0x5645_4B54_4F52_0F40


@pytest_asyncio.fixture(scope="module")
async def pg_engine() -> AsyncGenerator[AsyncEngine, None]:
    assert TEST_PG_DSN is not None
    engine = create_async_engine(TEST_PG_DSN, poolclass=NullPool)
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


async def _seed(
    sm: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    *,
    status: str = PROCESSING_STATUS_PENDING,
) -> uuid.UUID:
    file_id = uuid.uuid4()
    async with sm() as s:
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
            )
        )
        await s.commit()
    return file_id


async def _estado(
    sm: async_sessionmaker[AsyncSession], file_id: uuid.UUID
) -> tuple[str, uuid.UUID | None]:
    async with sm() as s:
        row = (
            await s.execute(
                select(UploadedFile.processing_status, UploadedFile.parse_attempt_id).where(
                    UploadedFile.id == file_id
                )
            )
        ).one()
    return row.processing_status, row.parse_attempt_id


def _s3_falla_con(exc: BaseException) -> Any:
    class _S3Roto:
        async def download(self, _key: str) -> bytes:
            raise exc

    return _S3Roto


def _client_error(code: str, status: int) -> ClientError:
    # `ClientError` tipa su respuesta con TypedDicts completos de botocore; acá
    # sólo interesan los dos campos que lee `_es_transitorio`.
    respuesta: Any = {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}
    return ClientError(respuesta, "GetObject")


# ── La liberación, aislada ────────────────────────────────────────────────────


async def test_libera_solo_si_sigue_siendo_el_dueno(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """Mismo fencing que el resto del ciclo: un intento que ya perdió el archivo
    no puede devolverlo a PENDING — sería sacárselo al dueño actual."""
    file_id = await _seed(sessionmaker, tenant_id)

    def _factory() -> AsyncSession:
        return sessionmaker()

    async with sessionmaker() as s:
        reclamado = await worker._claim_for_processing(s, str(file_id), str(tenant_id))
        assert reclamado is not None
        _, token = reclamado
        await s.commit()

    assert await worker._release_for_retry(_factory, str(file_id), str(tenant_id), token) is True
    estado, token_persistido = await _estado(sessionmaker, file_id)
    assert estado == PROCESSING_STATUS_PENDING
    assert token_persistido is None, "el token tiene que quedar limpio para el próximo intento"

    # Otro intento lo toma; el viejo ya no puede liberarlo.
    async with sessionmaker() as s:
        otro = await worker._claim_for_processing(s, str(file_id), str(tenant_id))
        assert otro is not None
        await s.commit()
    assert await worker._release_for_retry(_factory, str(file_id), str(tenant_id), token) is False
    estado, _ = await _estado(sessionmaker, file_id)
    assert estado == PROCESSING_STATUS_PROCESSING


async def test_no_libera_un_archivo_eliminado(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """Si lo borraron durante el intento, no hay nada que reencolar."""
    file_id = await _seed(sessionmaker, tenant_id)

    def _factory() -> AsyncSession:
        return sessionmaker()

    async with sessionmaker() as s:
        reclamado = await worker._claim_for_processing(s, str(file_id), str(tenant_id))
        assert reclamado is not None
        _, token = reclamado
        await s.commit()

    async with sessionmaker() as s:
        archivo = (
            await s.execute(select(UploadedFile).where(UploadedFile.id == file_id))
        ).scalar_one()
        archivo.deleted_at = datetime.now(UTC)
        await s.commit()

    assert await worker._release_for_retry(_factory, str(file_id), str(tenant_id), token) is False


# ── La task entera: qué estado queda según el tipo de error ──────────────────
#
# Estos tres son SÍNCRONOS a propósito: la task hace `asyncio.run(_run())`, que no
# se puede llamar desde adentro de un event loop ya corriendo. Por eso tampoco
# usan las fixtures async de arriba y arman su propio estado con `asyncio.run`.


def _correr(corutina: Any) -> Any:
    return asyncio.run(corutina)


async def _preparar(tenant_id: uuid.UUID) -> uuid.UUID:
    assert TEST_PG_DSN is not None
    engine = create_async_engine(TEST_PG_DSN, poolclass=NullPool)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    try:
        return await _seed(sm, tenant_id)
    finally:
        await engine.dispose()


async def _leer_estado(file_id: uuid.UUID) -> tuple[str, uuid.UUID | None]:
    assert TEST_PG_DSN is not None
    engine = create_async_engine(TEST_PG_DSN, poolclass=NullPool)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    try:
        return await _estado(sm, file_id)
    finally:
        await engine.dispose()


async def _limpiar(tenant_id: uuid.UUID) -> None:
    assert TEST_PG_DSN is not None
    engine = create_async_engine(TEST_PG_DSN, poolclass=NullPool)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sm() as s:
            await s.execute(delete(UploadedFile).where(UploadedFile.tenant_id == tenant_id))
            await s.execute(delete(Tenant).where(Tenant.tenant_id == tenant_id))
            await s.commit()
    finally:
        await engine.dispose()


@pytest.fixture
def _worker_contra_pg(monkeypatch: pytest.MonkeyPatch) -> None:
    """La task abre su propia sesión desde `settings.DATABASE_URL`; se la apunta al
    Postgres de prueba sin tocar la configuración global.

    El engine se crea con la función REAL, adentro del `asyncio.run` de la task:
    asyncpg liga sus conexiones al event loop que las abre, así que un engine
    fabricado en otro loop daría fallos intermitentes.
    """
    assert TEST_PG_DSN is not None
    dsn = TEST_PG_DSN
    original = worker._build_async_session
    monkeypatch.setattr(worker, "_build_async_session", lambda _url: original(dsn))


def _ejecutar_con_s3_roto(exc: BaseException, monkeypatch: pytest.MonkeyPatch) -> tuple[
    uuid.UUID, uuid.UUID
]:
    import app.integrations.s3 as s3_mod

    tenant_id = uuid.uuid4()
    file_id = _correr(_preparar(tenant_id))
    monkeypatch.setattr(s3_mod, "S3Client", _s3_falla_con(exc))
    return file_id, tenant_id


def test_error_transitorio_deja_el_archivo_reencolable(
    monkeypatch: pytest.MonkeyPatch, _worker_contra_pg: None
) -> None:
    """S3 devuelve 503: el archivo vuelve a PENDING, NO a FAILED.

    Llamada directa a la task: Celery pone `called_directly=True`, y ahí
    `self.retry(exc=...)` re-lanza la excepción original en vez de `Retry`. Lo que
    importa no es cuál de las dos sale, sino el estado que queda en la base — que
    es lo que decide si el reintento va a poder trabajar.
    """
    file_id, tenant_id = _ejecutar_con_s3_roto(_client_error("InternalError", 503), monkeypatch)
    try:
        with pytest.raises(ClientError):
            worker.process_spreadsheet(str(file_id), str(tenant_id))

        estado, token = _correr(_leer_estado(file_id))
        assert estado == PROCESSING_STATUS_PENDING, (
            "un error transitorio no puede dejar el archivo fuera del alcance del reintento"
        )
        assert token is None
    finally:
        _correr(_limpiar(tenant_id))


def test_error_permanente_cierra_en_failed(
    monkeypatch: pytest.MonkeyPatch, _worker_contra_pg: None
) -> None:
    """La clave no existe en S3: reintentar tres veces sólo retrasaría el FAILED."""
    file_id, tenant_id = _ejecutar_con_s3_roto(_client_error("NoSuchKey", 404), monkeypatch)
    try:
        with pytest.raises(ClientError):
            worker.process_spreadsheet(str(file_id), str(tenant_id))
        estado, _ = _correr(_leer_estado(file_id))
        assert estado == PROCESSING_STATUS_FAILED
    finally:
        _correr(_limpiar(tenant_id))


def test_error_de_parseo_cierra_en_failed(
    monkeypatch: pytest.MonkeyPatch, _worker_contra_pg: None
) -> None:
    """El archivo está roto: las tres veces va a estar igual de roto."""
    file_id, tenant_id = _ejecutar_con_s3_roto(ValueError("hoja ilegible"), monkeypatch)
    try:
        with pytest.raises(ValueError, match="hoja ilegible"):
            worker.process_spreadsheet(str(file_id), str(tenant_id))
        estado, _ = _correr(_leer_estado(file_id))
        assert estado == PROCESSING_STATUS_FAILED
    finally:
        _correr(_limpiar(tenant_id))
