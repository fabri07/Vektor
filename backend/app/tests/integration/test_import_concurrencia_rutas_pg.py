"""E6c-3 — `/confirm` y el ejecutor asíncrono no pueden importar a la vez.

Por qué este test y no alcanza con "comparten el código"
--------------------------------------------------------
El ejecutor llama a ``confirm_file``, así que en el papel las dos rutas usan el
mismo lease por archivo. Pero "usan la misma función" es una afirmación sobre el
código y lo que hay que garantizar es una propiedad del sistema: **dos
importaciones del mismo archivo, entrando por rutas distintas, no pueden escribir
las dos**. El token del intento no alcanza —protege contra dos EJECUTORES, no
contra el endpoint viejo— y por eso la exclusión tiene que ser la del archivo.

Contra Postgres real porque es lo único que puede probarlo: el lease es un CAS
atómico sobre una fila, y en SQLite en memoria cada test tiene su propia base.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.application.services.ingestion_lease_service import (
    acquire_import_lease,
    release_import_lease,
)
from app.persistence.models.business import BusinessProfile
from app.persistence.models.file import (
    PROCESSING_STATUS_IMPORTING,
    PROCESSING_STATUS_NEEDS_CONFIRMATION,
    UploadedFile,
)
from app.persistence.models.tenant import Tenant

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]


@pytest_asyncio.fixture(scope="module")
async def pg_engine() -> AsyncGenerator[AsyncEngine, None]:
    assert TEST_PG_DSN is not None
    engine = create_async_engine(TEST_PG_DSN, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def archivo(
    pg_engine: AsyncEngine,
) -> AsyncGenerator[tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID], None]:
    factory = async_sessionmaker(pg_engine, expire_on_commit=False)
    tenant_id, file_id = uuid.uuid4(), uuid.uuid4()
    async with factory() as s:
        s.add(Tenant(tenant_id=tenant_id, legal_name="T", display_name="T"))
        await s.flush()
        s.add(
            BusinessProfile(
                profile_id=uuid.uuid4(),
                tenant_id=tenant_id,
                vertical_code="kiosco_almacen",
                data_mode="M0",
                data_confidence="LOW",
                onboarding_completed=True,
            )
        )
        s.add(
            UploadedFile(
                id=file_id,
                tenant_id=tenant_id,
                original_filename="gastos.xlsx",
                s3_key=f"t/{file_id}.xlsx",
                content_type="text/csv",
                size_bytes=1,
                purpose="ingestion",
                status="uploaded",
                processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
            )
        )
        await s.commit()
    try:
        yield factory, tenant_id, file_id
    finally:
        async with factory() as s:
            await s.execute(delete(BusinessProfile).where(BusinessProfile.tenant_id == tenant_id))
            await s.execute(delete(UploadedFile).where(UploadedFile.tenant_id == tenant_id))
            await s.execute(delete(Tenant).where(Tenant.tenant_id == tenant_id))
            await s.commit()


async def test_solo_una_de_las_dos_rutas_se_lleva_el_lease_del_archivo(
    archivo: tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID],
) -> None:
    """El escenario que la exclusión compartida existe para cubrir.

    Un usuario aprieta "confirmar" (ruta vieja) mientras su importación asíncrona
    ya está corriendo (ruta nueva). Las dos van a pedir el MISMO lease por
    archivo, y sólo una puede llevárselo. Si cada ruta tuviera su propia
    exclusión, las dos pasarían y el archivo se importaría dos veces.
    """
    factory, tenant_id, file_id = archivo
    token_sincronico, token_asincronico = uuid.uuid4(), uuid.uuid4()

    async def _pedir(token: uuid.UUID) -> bool:
        async with factory() as s:
            return await acquire_import_lease(s, tenant_id, file_id, token)

    resultados = await asyncio.gather(
        _pedir(token_sincronico), _pedir(token_asincronico), return_exceptions=True
    )
    fallos = [r for r in resultados if isinstance(r, BaseException)]
    assert not fallos, f"ninguna de las dos puede reventar: {fallos}"

    ganadores = [r for r in resultados if r is True]
    assert len(ganadores) == 1, (
        f"{len(ganadores)} rutas se llevaron el lease del mismo archivo: las dos "
        "importarían en paralelo"
    )

    async with factory() as s:
        archivo_actual = await s.get(UploadedFile, file_id)
        assert archivo_actual is not None
        assert archivo_actual.processing_status == PROCESSING_STATUS_IMPORTING


async def test_el_ejecutor_no_puede_arrancar_si_confirm_ya_esta_importando(
    archivo: tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID],
) -> None:
    """El orden concreto que preocupa: la ruta vieja tomó el archivo primero.

    El token del INTENTO no protege de esto —protege contra dos ejecutores— así
    que si el ejecutor no pidiera el lease del archivo, entraría igual.
    """
    factory, tenant_id, file_id = archivo
    token_confirm = uuid.uuid4()
    async with factory() as s:
        assert await acquire_import_lease(s, tenant_id, file_id, token_confirm) is True

    async with factory() as s:
        tomado = await acquire_import_lease(s, tenant_id, file_id, uuid.uuid4())
    assert tomado is False, "el ejecutor entró sobre un archivo que ya estaba importando"


async def test_liberar_el_lease_deja_al_otro_entrar(
    archivo: tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID],
) -> None:
    """La exclusión es mutua, no un bloqueo permanente: cuando el primero termina
    o falla, el segundo puede correr."""
    factory, tenant_id, file_id = archivo
    primero = uuid.uuid4()
    async with factory() as s:
        assert await acquire_import_lease(s, tenant_id, file_id, primero) is True
    async with factory() as s:
        await release_import_lease(s, tenant_id, file_id, primero)

    async with factory() as s:
        assert await acquire_import_lease(s, tenant_id, file_id, uuid.uuid4()) is True


async def test_el_archivo_queda_en_un_solo_estado_despues_de_la_carrera(
    archivo: tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID],
) -> None:
    """Cinco pedidos simultáneos: uno gana, cuatro rebotan, y el archivo no queda
    en un estado intermedio raro.

    Se prueba con más de dos porque un CAS mal escrito puede serializar de a pares
    y dejar pasar al tercero.
    """
    factory, tenant_id, file_id = archivo

    async def _pedir() -> bool:
        async with factory() as s:
            return await acquire_import_lease(s, tenant_id, file_id, uuid.uuid4())

    resultados = await asyncio.gather(*[_pedir() for _ in range(5)])
    assert sum(1 for r in resultados if r) == 1

    async with factory() as s:
        cuantos = (
            await s.execute(
                select(func.count())
                .select_from(UploadedFile)
                .where(
                    UploadedFile.id == file_id,
                    UploadedFile.processing_status == PROCESSING_STATUS_IMPORTING,
                )
            )
        ).scalar_one()
    assert cuantos == 1
