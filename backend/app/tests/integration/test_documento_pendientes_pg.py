"""E8 sobre Postgres real — la captura de un documento SOBREVIVE al commit, y
una segunda pasada no la duplica.

Por qué acá y no en la suite de SQLite en memoria
--------------------------------------------------
Las dos afirmaciones que faltaban son sobre TRANSACCIONES, y ninguna de las dos
se puede hacer contra el arnés en memoria:

1. «Los pendientes siguen ahí al consultarlos desde una sesión nueva». En la
   suite, la sesión del test ES la del endpoint y cada conexión nueva de SQLite
   en memoria es una base distinta: no hay forma de leer desde afuera. Acá la
   lectura la hace una sesión propia contra la misma base, después del commit.
2. Que el ancla de la captura resista de verdad. En memoria, `HuellasDelArchivo`
   contesta desde su caché; lo que decide en producción es el UNIQUE de
   `operation_fingerprints` con el `ON CONFLICT DO NOTHING` del lote.

La segunda pasada representa la relectura, que es el caso real de re-entrada:
reprocesa el archivo llamando al mismo `insert_confirmed_data`.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, event, func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.application.services.file_parsing import parse_uploaded_content
from app.application.services.ingestion_import_service import insert_confirmed_data
from app.persistence.models.business import BusinessProfile
from app.persistence.models.file import UploadedFile
from app.persistence.models.memory import OperationFingerprint
from app.persistence.models.tenant import Tenant
from app.persistence.models.unclassified_record import UnclassifiedRecord

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]

#: Los dos primeros renglones son IDÉNTICOS: dos ventas legítimas, dos pendientes.
_DOCUMENTO = b"Venta Coca $1500\nVenta Coca $1500\nVenta Agua $800\n"


@pytest_asyncio.fixture(scope="module")
async def pg_engine() -> AsyncGenerator[AsyncEngine, None]:
    assert TEST_PG_DSN is not None
    engine = create_async_engine(TEST_PG_DSN, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def tenant_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest_asyncio.fixture
async def sm(
    pg_engine: AsyncEngine, tenant_id: uuid.UUID
) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    factory = async_sessionmaker(pg_engine, expire_on_commit=False, autoflush=False)
    try:
        yield factory
    finally:
        async with factory() as s:
            for modelo in (
                UnclassifiedRecord,
                OperationFingerprint,
                BusinessProfile,
                UploadedFile,
            ):
                await s.execute(delete(modelo).where(modelo.tenant_id == tenant_id))
            await s.execute(delete(Tenant).where(Tenant.tenant_id == tenant_id))
            await s.commit()


async def _pendientes(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> list[UnclassifiedRecord]:
    """Sesión NUEVA: nada de identity map ni de caché de la corrida anterior."""
    async with sm() as session:
        return list(
            (
                await session.execute(
                    select(UnclassifiedRecord).where(
                        UnclassifiedRecord.tenant_id == tenant_id
                    )
                )
            )
            .scalars()
            .all()
        )


async def test_los_pendientes_sobreviven_al_commit_y_no_se_duplican(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    summary: dict[str, Any] = parse_uploaded_content(
        _DOCUMENTO, "text/plain", "remito.txt"
    )
    upload_id = uuid.uuid4()

    async with sm() as session:
        session.add(Tenant(tenant_id=tenant_id, legal_name="T", display_name="T"))
        await session.flush()
        session.add(
            BusinessProfile(
                profile_id=uuid.uuid4(),
                tenant_id=tenant_id,
                vertical_code="kiosco_almacen",
                data_mode="M0",
                data_confidence="LOW",
                onboarding_completed=True,
            )
        )
        session.add(
            UploadedFile(
                id=upload_id,
                tenant_id=tenant_id,
                original_filename="remito.txt",
                s3_key=f"tests/{upload_id}.txt",
                content_type="text/plain",
                size_bytes=len(_DOCUMENTO),
                purpose="ingestion",
                status="uploaded",
                processing_status="DONE",
            )
        )
        await session.commit()

    async with sm() as session:
        counts = await insert_confirmed_data(
            session,
            tenant_id,
            summary,
            {"ventas": True},
            uploaded_file_id=upload_id,
        )
        await session.commit()
    assert counts["otros"] == 3, counts

    # 1. Están, leídos desde afuera de la transacción que los escribió.
    pendientes = await _pendientes(sm, tenant_id)
    assert len(pendientes) == 3
    assert sorted(str((p.row_data or {}).get("linea")) for p in pendientes) == [
        "Venta Agua $800",
        "Venta Coca $1500",
        "Venta Coca $1500",
    ], "dos renglones idénticos son dos operaciones: el texto no es la identidad"

    ids_originales = {p.id for p in pendientes}

    # 2. La segunda pasada (la relectura) no vuelve a capturar ninguna.
    async with sm() as session:
        recuento = await insert_confirmed_data(
            session,
            tenant_id,
            summary,
            {"ventas": True},
            uploaded_file_id=upload_id,
            source="reread",
        )
        await session.commit()
    assert recuento["otros"] == 0, recuento
    assert recuento["otros_ya_capturados"] == 3, recuento

    de_nuevo = await _pendientes(sm, tenant_id)
    assert {p.id for p in de_nuevo} == ids_originales, (
        "la segunda pasada dejó pendientes nuevos: el usuario tendría que "
        "clasificar los mismos renglones otra vez"
    )

    # 3. Y las huellas quedaron persistidas (una por ocurrencia), no en memoria.
    async with sm() as session:
        huellas = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(OperationFingerprint)
                    .where(OperationFingerprint.tenant_id == tenant_id)
                )
            ).scalar_one()
        )
    assert huellas == 3


async def test_una_caida_no_deja_ni_pendientes_ni_huellas_a_medias(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """Todo o nada: la corrida que no commitea no deja rastro, y el reintento sí.

    La huella se persiste en la MISMA transacción que la captura, así que una
    caída entre las dos no puede dejar el peor estado posible —el ancla quemada
    sin pendiente— que haría desaparecer el renglón para siempre: el reintento lo
    saltearía por "ya capturado" y en la bandeja no habría nada.
    """
    summary: dict[str, Any] = parse_uploaded_content(
        _DOCUMENTO, "text/plain", "remito.txt"
    )
    upload_id = uuid.uuid4()

    async with sm() as session:
        session.add(Tenant(tenant_id=tenant_id, legal_name="T", display_name="T"))
        await session.flush()
        session.add(
            BusinessProfile(
                profile_id=uuid.uuid4(),
                tenant_id=tenant_id,
                vertical_code="kiosco_almacen",
                data_mode="M0",
                data_confidence="LOW",
                onboarding_completed=True,
            )
        )
        session.add(
            UploadedFile(
                id=upload_id,
                tenant_id=tenant_id,
                original_filename="remito.txt",
                s3_key=f"tests/{upload_id}.txt",
                content_type="text/plain",
                size_bytes=len(_DOCUMENTO),
                purpose="ingestion",
                status="uploaded",
                processing_status="DONE",
            )
        )
        await session.commit()

    # La corrida que se cae: escribe y nunca commitea (lo que hace el ejecutor
    # ante cualquier excepción, ver `import_executor_worker`).
    async with sm() as session:
        await insert_confirmed_data(
            session, tenant_id, summary, {"ventas": True}, uploaded_file_id=upload_id
        )
        await session.rollback()

    assert await _pendientes(sm, tenant_id) == []
    async with sm() as session:
        huellas = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(OperationFingerprint)
                    .where(OperationFingerprint.tenant_id == tenant_id)
                )
            ).scalar_one()
        )
    assert huellas == 0, "el ancla quedó quemada sin pendiente que la respalde"

    # El reintento captura las tres, como si la caída no hubiera pasado.
    async with sm() as session:
        counts = await insert_confirmed_data(
            session, tenant_id, summary, {"ventas": True}, uploaded_file_id=upload_id
        )
        await session.commit()
    assert counts["otros"] == 3
    assert len(await _pendientes(sm, tenant_id)) == 3


async def test_las_anclas_del_documento_se_consultan_en_lote(
    sm: async_sessionmaker[AsyncSession], pg_engine: AsyncEngine, tenant_id: uuid.UUID
) -> None:
    """Preguntar de a una línea sería el mismo N+1 que E6c-1 desactivó.

    La compuerta mira la FORMA del SQL y el ORDEN de magnitud, no un número
    exacto: lo que no puede pasar es que el costo de consultar huellas crezca con
    la cantidad de renglones del documento.
    """
    lineas = 150
    contenido = b"".join(
        f"Venta producto {i} $1500\n".encode() for i in range(lineas)
    )
    summary: dict[str, Any] = parse_uploaded_content(
        contenido, "text/plain", "largo.txt"
    )
    upload_id = uuid.uuid4()

    async with sm() as session:
        session.add(Tenant(tenant_id=tenant_id, legal_name="T", display_name="T"))
        await session.flush()
        session.add(
            BusinessProfile(
                profile_id=uuid.uuid4(),
                tenant_id=tenant_id,
                vertical_code="kiosco_almacen",
                data_mode="M0",
                data_confidence="LOW",
                onboarding_completed=True,
            )
        )
        session.add(
            UploadedFile(
                id=upload_id,
                tenant_id=tenant_id,
                original_filename="largo.txt",
                s3_key=f"tests/{upload_id}.txt",
                content_type="text/plain",
                size_bytes=len(contenido),
                purpose="ingestion",
                status="uploaded",
                processing_status="DONE",
            )
        )
        await session.commit()

    consultas: list[str] = []

    @event.listens_for(pg_engine.sync_engine, "before_cursor_execute")
    def _capturar(conn: Any, cursor: Any, statement: Any, *rest: Any) -> None:
        texto = " ".join(str(statement).split())
        if "operation_fingerprints" in texto and texto.lower().startswith("select"):
            consultas.append(texto)

    try:
        async with sm() as session:
            counts = await insert_confirmed_data(
                session,
                tenant_id,
                summary,
                {"ventas": True},
                uploaded_file_id=upload_id,
            )
            await session.commit()
    finally:
        event.remove(pg_engine.sync_engine, "before_cursor_execute", _capturar)

    assert counts["otros"] == lineas
    assert consultas, "sin consultar huellas no hay ancla que valga"
    assert all(" in (" in q.lower() for q in consultas), (
        f"hay consultas sin acotar por las anclas del archivo: {consultas}"
    )
    assert len(consultas) < lineas / 10, (
        f"{len(consultas)} consultas para {lineas} líneas: se está preguntando "
        "de a una"
    )
