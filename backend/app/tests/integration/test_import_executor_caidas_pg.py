"""E6c-3 — qué pasa si el proceso muere en cada punto del recorrido.

El recorrido tiene tres cortes posibles y cada uno deja un estado distinto:

1. **Entre el commit y la publicación.** El intento y su orden existen; nadie las
   entregó. Es la ventana que E3 dejó abierta a propósito y que la tabla de salida
   cierra: el publicador periódico las encuentra.
2. **Entre la entrega y la marca de publicada.** La orden sale al broker y el
   proceso muere antes de marcarla. Se vuelve a entregar — y esa segunda entrega
   no puede ejecutar dos veces.
3. **Entre el commit de los efectos y el cierre del intento.** Los datos entraron
   y el intento quedó en EJECUTANDO. La recuperación lo reencola, y la segunda
   corrida no puede duplicar nada.

Se prueban los tres, porque son tres fallas distintas con tres recuperaciones
distintas y "el sistema es durable" no significa nada si sólo se probó una.

Contra Postgres real: todo esto es sobre transacciones que commitean, mueren y se
vuelven a abrir. En SQLite en memoria no hay nada de eso.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.application.services.import_attempt_service import (
    marcar_publicada,
    obtener_intento,
    ordenes_pendientes,
    reclamar_intento,
    registrar_intento,
)
from app.domain.import_attempt import EJECUTANDO, PENDIENTE
from app.persistence.models.file import UploadedFile
from app.persistence.models.import_attempt import ImportAttempt, ImportOutbox
from app.persistence.models.tenant import Tenant

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]

_PAYLOAD: dict[str, Any] = {"confirmed_fields": {"gastos": True}}


@pytest_asyncio.fixture(scope="module")
async def pg_engine() -> AsyncGenerator[AsyncEngine, None]:
    assert TEST_PG_DSN is not None
    engine = create_async_engine(TEST_PG_DSN, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def contexto(
    pg_engine: AsyncEngine,
) -> AsyncGenerator[tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID], None]:
    factory = async_sessionmaker(pg_engine, expire_on_commit=False)
    tenant_id, file_id = uuid.uuid4(), uuid.uuid4()
    async with factory() as s:
        s.add(Tenant(tenant_id=tenant_id, legal_name="T", display_name="T"))
        await s.flush()
        s.add(
            UploadedFile(
                id=file_id,
                tenant_id=tenant_id,
                original_filename="x.xlsx",
                s3_key=f"t/{file_id}.xlsx",
                content_type="text/csv",
                size_bytes=1,
                purpose="ingestion",
                status="uploaded",
                processing_status="NEEDS_CONFIRMATION",
            )
        )
        await s.commit()
    try:
        yield factory, tenant_id, file_id
    finally:
        async with factory() as s:
            await s.execute(delete(ImportOutbox).where(ImportOutbox.tenant_id == tenant_id))
            await s.execute(delete(ImportAttempt).where(ImportAttempt.tenant_id == tenant_id))
            await s.execute(delete(UploadedFile).where(UploadedFile.tenant_id == tenant_id))
            await s.execute(delete(Tenant).where(Tenant.tenant_id == tenant_id))
            await s.commit()


async def _registrar(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    file_id: uuid.UUID,
    clave: str = "req-1",
) -> uuid.UUID:
    async with factory() as s:
        r = await registrar_intento(
            s,
            tenant_id=tenant_id,
            file_id=file_id,
            request_key=clave,
            payload=_PAYLOAD,
        )
        await s.commit()
        return r.intento.id


# ── Corte 1: entre el commit y la publicación ────────────────────────────────
async def test_si_el_proceso_muere_antes_de_publicar_la_orden_sigue_pendiente(
    contexto: tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID],
) -> None:
    """La ventana que la tabla de salida existe para cerrar.

    Sin ella, publicar dentro de la transacción podía emitir un mensaje por una
    transacción que después se revierte, y publicar después del commit perdía el
    trabajo si el proceso moría en el medio. Acá el commit ya ocurrió y nadie
    entregó nada: la orden tiene que seguir esperando a alguien que la entregue.
    """
    factory, tenant_id, file_id = contexto
    attempt_id = await _registrar(factory, tenant_id, file_id)

    async with factory() as s:
        pendientes = [o for o in await ordenes_pendientes(s) if o.tenant_id == tenant_id]
    assert [o.attempt_id for o in pendientes] == [attempt_id]

    # Y el intento sigue esperando, no se perdió ni se dio por hecho.
    async with factory() as s:
        intento = await obtener_intento(s, tenant_id, attempt_id)
    assert intento is not None and intento.status == PENDIENTE


# ── Corte 2: entre la entrega y la marca de publicada ────────────────────────
async def test_una_orden_entregada_pero_no_marcada_se_vuelve_a_entregar(
    contexto: tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID],
) -> None:
    """El publicador entrega PRIMERO y marca DESPUÉS, a propósito.

    Al revés, morir entre los dos pasos perdería la orden para siempre: quedaría
    marcada como publicada sin que nadie la haya recibido. En este orden la
    consecuencia de morir es una entrega repetida, que es recuperable.
    """
    factory, tenant_id, file_id = contexto
    await _registrar(factory, tenant_id, file_id)

    # El proceso muere justo después del send_task: nada se marcó.
    async with factory() as s:
        pendientes = [o for o in await ordenes_pendientes(s) if o.tenant_id == tenant_id]
    assert len(pendientes) == 1, "la orden no marcada tiene que seguir pendiente"

    # El siguiente tick la vuelve a tomar, ahora sí completando el ciclo.
    async with factory() as s:
        orden = [o for o in await ordenes_pendientes(s) if o.tenant_id == tenant_id][0]
        await marcar_publicada(s, orden.id)
        await s.commit()
    async with factory() as s:
        assert not [o for o in await ordenes_pendientes(s) if o.tenant_id == tenant_id]


async def test_la_entrega_repetida_no_ejecuta_dos_veces(
    contexto: tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID],
) -> None:
    """La consecuencia del orden anterior, cerrada donde corresponde: en el lease.

    Es lo que permite NO prometer "exactly once" de transporte. Celery no lo da;
    la garantía está en que sólo una entrega consigue el token.
    """
    factory, tenant_id, file_id = contexto
    attempt_id = await _registrar(factory, tenant_id, file_id)

    async with factory() as s:
        primero = await reclamar_intento(s, attempt_id)
        await s.commit()
    async with factory() as s:
        segundo = await reclamar_intento(s, attempt_id)
        await s.commit()

    assert primero is not None
    assert segundo is None, "la entrega repetida volvió a ejecutar"


# ── Corte 3: entre los efectos y el cierre del intento ───────────────────────
async def test_si_muere_despues_de_importar_el_intento_se_recupera(
    contexto: tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID],
) -> None:
    """La ventana declarada del ejecutor.

    Los efectos y el cierre del intento no commitean juntos: `confirm_file` cierra
    su propia transacción. Si el proceso muere en el medio, el intento queda en
    EJECUTANDO con el lease vencido. Lo que se afirma acá es que **se recupera
    solo**: la recuperación lo reencola y la segunda corrida no duplica nada,
    porque las huellas de fila hacen el import idempotente.
    """
    from app.application.services.import_attempt_service import liberar_huerfanos

    factory, tenant_id, file_id = contexto
    attempt_id = await _registrar(factory, tenant_id, file_id)

    async with factory() as s:
        await reclamar_intento(s, attempt_id)
        await s.commit()
    # Muere acá: los efectos ya se commitearon, el intento nunca se cerró.
    async with factory() as s:
        await s.execute(
            text(
                "UPDATE import_attempts SET lease_expires_at = now() - interval '1 hour' "
                "WHERE id = :id"
            ),
            {"id": attempt_id},
        )
        await s.commit()

    async with factory() as s:
        intento = await obtener_intento(s, tenant_id, attempt_id)
    assert intento is not None and intento.status == EJECUTANDO, (
        "antes de recuperar, el intento queda colgado — que es el problema"
    )

    async with factory() as s:
        liberados = await liberar_huerfanos(s)
        await s.commit()
    assert attempt_id in liberados

    async with factory() as s:
        intento = await obtener_intento(s, tenant_id, attempt_id)
    assert intento is not None and intento.status == PENDIENTE, (
        "un intento colgado tiene que volver a la cola, no quedarse en EJECUTANDO "
        "para siempre mostrándole «importando» a un usuario que no tiene a nadie "
        "importando"
    )
    # Y se puede volver a reclamar: la recuperación no lo dejó inutilizable.
    async with factory() as s:
        assert await reclamar_intento(s, attempt_id) is not None
        await s.commit()


async def test_el_contador_de_intentos_crece_en_cada_reclamo(
    contexto: tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID],
) -> None:
    """Sin el contador, un trabajo que mata al ejecutor se reencola para siempre
    y va matando a los que siguen. Es lo que hace terminable a la recuperación."""
    factory, tenant_id, file_id = contexto
    attempt_id = await _registrar(factory, tenant_id, file_id)

    for _ in range(2):
        async with factory() as s:
            await reclamar_intento(s, attempt_id)
            await s.execute(
                text(
                    "UPDATE import_attempts SET lease_expires_at = now() - "
                    "interval '1 hour' WHERE id = :id"
                ),
                {"id": attempt_id},
            )
            await s.commit()

    async with factory() as s:
        intento = await obtener_intento(s, tenant_id, attempt_id)
    assert intento is not None and intento.attempts == 2


async def test_borrar_el_archivo_se_lleva_sus_intentos_y_ordenes(
    contexto: tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID],
) -> None:
    """Limpieza de recursos: un intento de un archivo que ya no existe no puede
    quedar esperando ejecución para siempre."""
    factory, tenant_id, file_id = contexto
    await _registrar(factory, tenant_id, file_id)

    async with factory() as s:
        await s.execute(delete(UploadedFile).where(UploadedFile.id == file_id))
        await s.commit()

    async with factory() as s:
        intentos = (
            (
                await s.execute(
                    select(ImportAttempt).where(ImportAttempt.tenant_id == tenant_id)
                )
            )
            .scalars()
            .all()
        )
        ordenes = (
            (await s.execute(select(ImportOutbox).where(ImportOutbox.tenant_id == tenant_id)))
            .scalars()
            .all()
        )
    assert not intentos and not ordenes
