"""H15 — el worker de parseo no puede pisar un estado que no le pertenece.

Qué se protege
--------------
Antes, ``_load_and_lock`` hacía un ``SELECT`` plano —sin ``FOR UPDATE``, pese al
nombre— y asignaba ``processing_status = PROCESSING`` de forma incondicional: sin
guard de estado, sin ``rowcount`` y sin filtrar ``deleted_at``. Con
``task_acks_late=True`` una re-entrega bastaba para llevar un archivo ``DONE`` de
vuelta a ``PROCESSING`` y de ahí a ``NEEDS_CONFIRMATION``, que es un estado que el
CAS de ``acquire_import_lease`` acepta.

El ciclo tiene DOS puntos de escritura y hacen falta los dos:

* la **adquisición** (``_claim_for_processing``), que sólo puede tomar un archivo
  ``PENDING`` y no borrado;
* la **escritura del resultado** (``_save_result``), que exige el token del
  intento. Un ``WHERE processing_status = 'PROCESSING'`` no alcanzaría: el camino
  de recuperación (``reprocess_file`` devuelve a ``PENDING`` lo trabado y reencola)
  deja legítimamente DOS intentos en ese estado, y sin token el ``FAILED`` tardío
  del worker viejo pisa el resultado del que sí terminó.

Por qué contra Postgres real
----------------------------
La garantía es que de dos ``UPDATE`` concurrentes sobre la misma fila exactamente
uno vea ``rowcount == 1``: eso lo decide el row-lock del motor, y SQLite —con su
lock de base entera y sin concurrencia real entre conexiones— no puede
ejercitarlo. Las dos sesiones acá son conexiones físicas distintas (``NullPool``).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import cast

import pytest
import pytest_asyncio
from sqlalchemy import Table, delete, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.jobs.ingestion_worker import (
    ParseOwnershipLostError,
    _claim_for_processing,
    _load_owned,
    _save_result,
)
from app.persistence.db.base import Base
from app.persistence.models.file import (
    PROCESSING_STATUS_DONE,
    PROCESSING_STATUS_FAILED,
    PROCESSING_STATUS_NEEDS_CONFIRMATION,
    PROCESSING_STATUS_PENDING,
    PROCESSING_STATUS_PROCESSING,
    PROCESSING_STATUS_REJECTED,
    UploadedFile,
)
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]

#: Misma clave que el resto de los tests PG de ingestión: serializa el CREATE
#: entre workers de xdist.
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


async def _seed_file(
    sm: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    *,
    status: str = PROCESSING_STATUS_PENDING,
    deleted: bool = False,
) -> uuid.UUID:
    file_id = uuid.uuid4()
    async with sm() as s:
        # Flush del tenant ANTES del archivo: sin relationship() la unit-of-work
        # no ordena sola el INSERT del padre → FK violation si van juntos.
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
                deleted_at=datetime.now(UTC) if deleted else None,
            )
        )
        await s.commit()
    return file_id


async def _estado(
    sm: async_sessionmaker[AsyncSession], file_id: uuid.UUID
) -> tuple[str, uuid.UUID | None, dict | None]:
    async with sm() as s:
        row = (
            await s.execute(
                select(
                    UploadedFile.processing_status,
                    UploadedFile.parse_attempt_id,
                    UploadedFile.parsed_summary_json,
                ).where(UploadedFile.id == file_id)
            )
        ).one()
    return row.processing_status, row.parse_attempt_id, row.parsed_summary_json


async def _borrar(sm: async_sessionmaker[AsyncSession], file_id: uuid.UUID) -> None:
    """Soft-delete del archivo, en su propia transacción."""
    async with sm() as s:
        archivo = (
            await s.execute(select(UploadedFile).where(UploadedFile.id == file_id))
        ).scalar_one()
        archivo.deleted_at = datetime.now(UTC)
        await s.commit()


async def _claim_en_sesion_propia(
    sm: async_sessionmaker[AsyncSession], file_id: uuid.UUID, tenant_id: uuid.UUID
) -> uuid.UUID | None:
    """Adquiere en su PROPIA conexión y commitea. Devuelve el token, o None."""
    async with sm() as s:
        reclamado = await _claim_for_processing(s, str(file_id), str(tenant_id))
        await s.commit()
    return None if reclamado is None else reclamado[1]


# ── 1. Adquisición concurrente ────────────────────────────────────────────────


async def test_dos_workers_simultaneos_solo_uno_adquiere(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """Dos workers que salen a la vez por el mismo archivo: gana exactamente uno.

    Es la garantía que el ``SELECT`` plano no daba — los dos leían ``PENDING``,
    los dos escribían ``PROCESSING`` y los dos seguían de largo creyéndose dueños.
    """
    file_id = await _seed_file(sessionmaker, tenant_id)

    tokens = await asyncio.gather(
        _claim_en_sesion_propia(sessionmaker, file_id, tenant_id),
        _claim_en_sesion_propia(sessionmaker, file_id, tenant_id),
    )

    ganadores = [t for t in tokens if t is not None]
    assert len(ganadores) == 1, f"esperaba un único ganador, hubo {len(ganadores)}"

    estado, token_persistido, _ = await _estado(sessionmaker, file_id)
    assert estado == PROCESSING_STATUS_PROCESSING
    # El token en la fila es el del ganador: el perdedor no escribió nada.
    assert token_persistido == ganadores[0]


# ── 2. Mensaje repetido sobre un archivo ya terminado ─────────────────────────


@pytest.mark.parametrize(
    "estado_final",
    [
        PROCESSING_STATUS_DONE,
        PROCESSING_STATUS_NEEDS_CONFIRMATION,
        PROCESSING_STATUS_REJECTED,
    ],
)
async def test_mensaje_repetido_no_revive_un_archivo_terminado(
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    estado_final: str,
) -> None:
    """La re-entrega de `task_acks_late` no puede devolver a PROCESSING lo terminado.

    ``DONE`` es el caso peligroso: volvía a ``NEEDS_CONFIRMATION``, que es un
    estado que el CAS de ``acquire_import_lease`` acepta.
    """
    file_id = await _seed_file(sessionmaker, tenant_id, status=estado_final)

    assert await _claim_en_sesion_propia(sessionmaker, file_id, tenant_id) is None

    estado, token, _ = await _estado(sessionmaker, file_id)
    assert estado == estado_final
    assert token is None


# ── 3. Archivo eliminado ──────────────────────────────────────────────────────


async def test_archivo_eliminado_no_se_resucita(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """Un archivo con ``deleted_at`` no es reclamable — mismo criterio que el lease."""
    file_id = await _seed_file(sessionmaker, tenant_id, deleted=True)

    assert await _claim_en_sesion_propia(sessionmaker, file_id, tenant_id) is None

    estado, token, _ = await _estado(sessionmaker, file_id)
    assert estado == PROCESSING_STATUS_PENDING
    assert token is None


# ── 3b. Eliminado DESPUÉS del claim ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("estado_final", "summary_final"),
    [
        (PROCESSING_STATUS_NEEDS_CONFIRMATION, {"file_type": "spreadsheet", "rows_processed": 7}),
        (PROCESSING_STATUS_REJECTED, {"file_type": "spreadsheet"}),
        (PROCESSING_STATUS_FAILED, {"error": "boom", "file_type": "spreadsheet"}),
    ],
)
async def test_eliminado_despues_del_claim_no_recibe_ningun_resultado(
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    estado_final: str,
    summary_final: dict,
) -> None:
    """El borrado puede llegar DESPUÉS de reclamar, y ahí el guard del claim ya pasó.

    El archivo se elimina con el parseo en curso: el worker sigue siendo su dueño
    —el token es el suyo y el estado sigue siendo ``PROCESSING``—, así que ni el
    token ni el estado lo frenan. Sin ``deleted_at`` en la escritura, los tres
    finales entraban y le cambiaban estado y contenido a un archivo ya borrado.
    """
    file_id = await _seed_file(sessionmaker, tenant_id)
    async with sessionmaker() as s:
        reclamado = await _claim_for_processing(s, str(file_id), str(tenant_id))
        assert reclamado is not None
        record, token = reclamado
        await s.commit()

    await _borrar(sessionmaker, file_id)

    # Ni releer ni escribir: la lectura corta temprano, la escritura es el fencing.
    async with sessionmaker() as s:
        with pytest.raises(ParseOwnershipLostError):
            await _load_owned(s, str(file_id), str(tenant_id), token)
    async with sessionmaker() as s:
        with pytest.raises(ParseOwnershipLostError):
            await _save_result(s, record, summary_final, estado_final, token=token)
        await s.rollback()

    estado, _, summary = await _estado(sessionmaker, file_id)
    assert estado == PROCESSING_STATUS_PROCESSING
    assert summary is None


async def test_eliminado_entre_load_owned_y_save_result(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """La ventana que demuestra DÓNDE tiene que estar la condición.

    Acá ``_load_owned`` se ejecuta ANTES del borrado, así que pasa: el archivo
    todavía estaba vivo. Es exactamente el caso que un guard puesto sólo en la
    lectura dejaría pasar — y el que prueba que la protección tiene que vivir en
    el ``UPDATE``, que es lo único que corre en la misma sentencia que la
    escritura.
    """
    file_id = await _seed_file(sessionmaker, tenant_id)
    async with sessionmaker() as s:
        reclamado = await _claim_for_processing(s, str(file_id), str(tenant_id))
        assert reclamado is not None
        _, token = reclamado
        await s.commit()

    # La lectura ocurre con el archivo todavía vivo y NO falla.
    async with sessionmaker() as s:
        record = await _load_owned(s, str(file_id), str(tenant_id), token)
        await s.commit()

    # El borrado llega después, en la ventana entre leer y escribir.
    await _borrar(sessionmaker, file_id)

    async with sessionmaker() as s:
        with pytest.raises(ParseOwnershipLostError):
            await _save_result(
                s,
                record,
                {"file_type": "spreadsheet", "rows_processed": 7},
                PROCESSING_STATUS_NEEDS_CONFIRMATION,
                token=token,
            )
        await s.rollback()

    estado, _, summary = await _estado(sessionmaker, file_id)
    assert estado == PROCESSING_STATUS_PROCESSING
    assert summary is None


# ── 4. Worker viejo que perdió la propiedad ───────────────────────────────────


async def test_worker_viejo_no_escribe_con_el_archivo_tomado_por_otro(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """El escenario exacto para el que existe el token.

    Reproduce el camino de recuperación real: A se cuelga, ``reprocess_file`` lo
    devuelve a ``PENDING`` por staleness y reencola, B lo reclama. En ese momento
    el archivo está en ``PROCESSING`` **y hay dos intentos que se creen dueños**.

    Acá es donde un fencing hecho a medias —``WHERE processing_status =
    'PROCESSING'`` sin token— deja pasar al viejo: el estado coincide, así que su
    escritura tardía prospera y le borra a B el trabajo. Se afirma antes de que B
    escriba nada, para que sea el token y sólo el token lo que rechaza a A.
    """
    file_id = await _seed_file(sessionmaker, tenant_id)

    async with sessionmaker() as s:
        reclamado_a = await _claim_for_processing(s, str(file_id), str(tenant_id))
        assert reclamado_a is not None
        record_a, token_a = reclamado_a
        await s.commit()

    # `reprocess_file` devuelve a PENDING lo trabado (staleness > 300 s) y reencola.
    async with sessionmaker() as s:
        trabado = (
            await s.execute(select(UploadedFile).where(UploadedFile.id == file_id))
        ).scalar_one()
        trabado.processing_status = PROCESSING_STATUS_PENDING
        trabado.parsed_summary_json = None
        await s.commit()

    token_b = await _claim_en_sesion_propia(sessionmaker, file_id, tenant_id)
    assert token_b is not None and token_b != token_a

    # El archivo está en PROCESSING, y su dueño es B. A todavía no se enteró.
    estado, token, _ = await _estado(sessionmaker, file_id)
    assert estado == PROCESSING_STATUS_PROCESSING
    assert token == token_b

    # A no puede ni releer ni escribir, PESE a que el estado es el que él espera.
    async with sessionmaker() as s:
        with pytest.raises(ParseOwnershipLostError):
            await _load_owned(s, str(file_id), str(tenant_id), token_a)
    async with sessionmaker() as s:
        with pytest.raises(ParseOwnershipLostError):
            await _save_result(
                s,
                record_a,
                {"error": "boom", "file_type": "spreadsheet"},
                PROCESSING_STATUS_FAILED,
                token=token_a,
            )
        await s.rollback()

    # Nada se movió: B sigue siendo el dueño y su parseo sigue en curso.
    estado, token, summary = await _estado(sessionmaker, file_id)
    assert estado == PROCESSING_STATUS_PROCESSING
    assert token == token_b
    assert summary is None

    # Y B termina normalmente.
    async with sessionmaker() as s:
        record_b = await _load_owned(s, str(file_id), str(tenant_id), token_b)
        await _save_result(
            s,
            record_b,
            {"file_type": "spreadsheet", "rows_processed": 7},
            PROCESSING_STATUS_NEEDS_CONFIRMATION,
            token=token_b,
        )
        await s.commit()

    estado, _, summary = await _estado(sessionmaker, file_id)
    assert estado == PROCESSING_STATUS_NEEDS_CONFIRMATION
    assert summary == {"file_type": "spreadsheet", "rows_processed": 7}


# ── 5. Escrituras de resultado tardías: éxito, rechazo Y error ────────────────


@pytest.mark.parametrize(
    ("estado_tardio", "summary_tardio"),
    [
        (PROCESSING_STATUS_FAILED, {"error": "boom", "file_type": "spreadsheet"}),
        (PROCESSING_STATUS_NEEDS_CONFIRMATION, {"file_type": "spreadsheet", "rows_processed": 1}),
        (PROCESSING_STATUS_REJECTED, {"file_type": "spreadsheet"}),
    ],
)
async def test_ningun_final_tardio_pisa_al_dueno_vigente(
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    estado_tardio: str,
    summary_tardio: dict,
) -> None:
    """Los tres finales pasan por el mismo fencing, no sólo el de éxito.

    Un ``FAILED`` tardío es tan corrupto como un ``NEEDS_CONFIRMATION`` tardío:
    borra un resultado que puede estar bien. El archivo queda en ``PROCESSING``
    con otro dueño a propósito — si se lo dejara en ``DONE``, el ``WHERE`` del
    estado alcanzaría y el test no diría nada sobre el token.
    """
    file_id = await _seed_file(sessionmaker, tenant_id)
    async with sessionmaker() as s:
        reclamado = await _claim_for_processing(s, str(file_id), str(tenant_id))
        assert reclamado is not None
        record_viejo, token_viejo = reclamado
        await s.commit()

    async with sessionmaker() as s:
        vuelto = (
            await s.execute(select(UploadedFile).where(UploadedFile.id == file_id))
        ).scalar_one()
        vuelto.processing_status = PROCESSING_STATUS_PENDING
        await s.commit()
    token_vigente = await _claim_en_sesion_propia(sessionmaker, file_id, tenant_id)
    assert token_vigente is not None

    async with sessionmaker() as s:
        with pytest.raises(ParseOwnershipLostError):
            await _save_result(s, record_viejo, summary_tardio, estado_tardio, token=token_viejo)
        await s.rollback()

    estado, token, summary = await _estado(sessionmaker, file_id)
    assert estado == PROCESSING_STATUS_PROCESSING
    assert token == token_vigente
    assert summary is None


# ── 6. El fencing no es sólo del estado: un token viejo tampoco revive ────────


async def test_token_viejo_no_escribe_sobre_un_archivo_ya_terminado(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """La otra mitad del ``WHERE``: el estado. Un token cuyo archivo ya salió de
    ``PROCESSING`` por otra vía (un confirm que lo dejó en ``DONE``) tampoco
    escribe, aunque el token siga siendo el último que se guardó."""
    file_id = await _seed_file(sessionmaker, tenant_id)
    async with sessionmaker() as s:
        reclamado = await _claim_for_processing(s, str(file_id), str(tenant_id))
        assert reclamado is not None
        record, token = reclamado
        await s.commit()

    async with sessionmaker() as s:
        confirmado = (
            await s.execute(select(UploadedFile).where(UploadedFile.id == file_id))
        ).scalar_one()
        confirmado.processing_status = PROCESSING_STATUS_DONE
        confirmado.parsed_summary_json = {"file_type": "spreadsheet", "importado": True}
        await s.commit()

    async with sessionmaker() as s:
        with pytest.raises(ParseOwnershipLostError):
            await _save_result(
                s,
                record,
                {"error": "boom", "file_type": "spreadsheet"},
                PROCESSING_STATUS_FAILED,
                token=token,
            )
        await s.rollback()

    estado, _, summary = await _estado(sessionmaker, file_id)
    assert estado == PROCESSING_STATUS_DONE
    assert summary == {"file_type": "spreadsheet", "importado": True}
