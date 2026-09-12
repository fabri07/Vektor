"""E6c-3 — el intento de importación, contra Postgres real.

Por qué acá y no en la suite de SQLite
---------------------------------------
Todo lo que este módulo garantiza es sobre CARRERAS: dos requests idénticos que
llegan a la vez, la misma orden entregada dos veces, un ejecutor que revive
después de perder su lease. En SQLite en memoria cada test tiene su propia base y
no hay concurrencia que medir; acá las transacciones son reales y el ``UPDATE``
con ``rowcount`` se serializa de verdad.

Las dos idempotencias, que se confunden seguido
------------------------------------------------
* **De la petición**: el usuario apretó dos veces o el cliente reintentó por
  timeout. Tiene que devolver **el mismo intento**. Lo da la clave de petición.
* **De la ejecución**: el transporte entregó la misma orden dos veces. Tiene que
  ejecutarse **una sola vez**. NO lo da la clave de petición — lo da el lease con
  token.

Un test que sólo probara la primera dejaría la segunda abierta, que es la que
duplica plata.
"""

from __future__ import annotations

import asyncio
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
    SolicitudEnConflictoError,
    cerrar_intento,
    liberar_huerfanos,
    obtener_intento,
    ordenes_pendientes,
    reclamar_intento,
    registrar_intento,
    renovar_lease,
)
from app.domain.import_attempt import COMPLETADO, EJECUTANDO, FALLADO, PENDIENTE
from app.persistence.models.file import UploadedFile
from app.persistence.models.import_attempt import ImportAttempt, ImportOutbox
from app.persistence.models.tenant import Tenant

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]

_PAYLOAD: dict[str, Any] = {
    "column_mappings": [{"source_column": "monto", "target_field": "amount"}],
    "confirmed_fields": {"gastos": True},
}


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
    *,
    clave: str = "req-1",
    payload: dict[str, Any] | None = None,
) -> tuple[uuid.UUID, bool]:
    async with factory() as s:
        resultado = await registrar_intento(
            s,
            tenant_id=tenant_id,
            file_id=file_id,
            request_key=clave,
            payload=payload or _PAYLOAD,
        )
        await s.commit()
        return resultado.intento.id, resultado.creado


# ── Idempotencia de la PETICIÓN ──────────────────────────────────────────────
async def test_repetir_la_misma_peticion_devuelve_el_mismo_intento(
    contexto: tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID],
) -> None:
    """El caso del timeout: el cliente reintenta y no puede crear otra importación."""
    factory, tenant_id, file_id = contexto
    primero, creado_1 = await _registrar(factory, tenant_id, file_id)
    segundo, creado_2 = await _registrar(factory, tenant_id, file_id)

    assert creado_1 is True
    assert creado_2 is False
    assert primero == segundo

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
    assert len(intentos) == 1, "una petición repetida creó una importación nueva"


async def test_dos_peticiones_identicas_simultaneas_crean_un_solo_intento(
    contexto: tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID],
) -> None:
    """Doble click. Las dos pasan el SELECT previo; el UNIQUE decide.

    Con un `if not existe: crear` los dos pasarían el chequeo y el segundo
    reventaría en el commit, cuando ya es tarde para responder algo útil.
    """
    factory, tenant_id, file_id = contexto
    resultados = await asyncio.gather(
        _registrar(factory, tenant_id, file_id),
        _registrar(factory, tenant_id, file_id),
        return_exceptions=True,
    )
    fallos = [r for r in resultados if isinstance(r, BaseException)]
    assert not fallos, f"ninguna de las dos puede reventar: {fallos}"

    ids = {r[0] for r in resultados if not isinstance(r, BaseException)}
    assert len(ids) == 1, f"se crearon {len(ids)} intentos para la misma clave"


async def test_la_misma_clave_con_otro_contenido_es_conflicto(
    contexto: tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID],
) -> None:
    """No se puede resolver solo: devolver el intento viejo importaría algo que el
    usuario no pidió, y crear uno nuevo rompería la promesa de la clave."""
    factory, tenant_id, file_id = contexto
    await _registrar(factory, tenant_id, file_id)

    with pytest.raises(SolicitudEnConflictoError):
        await _registrar(
            factory,
            tenant_id,
            file_id,
            payload={**_PAYLOAD, "confirmed_fields": {"ventas": True}},
        )


async def test_el_orden_de_las_claves_del_payload_no_cambia_la_huella(
    contexto: tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID],
) -> None:
    """El navegador no garantiza el orden de los campos de un JSON. Si el orden
    contara, el mismo confirm reenviado se vería como un contenido distinto y
    daría conflicto sobre sí mismo."""
    factory, tenant_id, file_id = contexto
    await _registrar(factory, tenant_id, file_id, payload={"a": 1, "b": 2})
    _, creado = await _registrar(factory, tenant_id, file_id, payload={"b": 2, "a": 1})
    assert creado is False


# ── La orden viaja con el intento, en la misma transacción ───────────────────
async def test_el_intento_y_su_orden_se_escriben_juntos(
    contexto: tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID],
) -> None:
    factory, tenant_id, file_id = contexto
    attempt_id, _ = await _registrar(factory, tenant_id, file_id)
    async with factory() as s:
        pendientes = await ordenes_pendientes(s)
    assert [o.attempt_id for o in pendientes if o.tenant_id == tenant_id] == [attempt_id]


async def test_si_la_transaccion_se_revierte_no_queda_ni_intento_ni_orden(
    contexto: tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID],
) -> None:
    """El commit es lo ÚNICO que decide. Es el punto de la tabla de salida: sin
    ella, un mensaje podía salir por una transacción que después se revertía."""
    factory, tenant_id, file_id = contexto
    async with factory() as s:
        await registrar_intento(
            s,
            tenant_id=tenant_id,
            file_id=file_id,
            request_key="se-revierte",
            payload=_PAYLOAD,
        )
        await s.rollback()

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


# ── Idempotencia de la EJECUCIÓN: el lease ───────────────────────────────────
async def test_la_misma_orden_entregada_dos_veces_se_ejecuta_una(
    contexto: tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID],
) -> None:
    """El publicador puede morir después de enviar y antes de marcar la orden.

    La segunda entrega no encuentra nada que reclamar. Es lo que permite NO
    prometer "exactly once" de transporte: la garantía está acá.
    """
    factory, tenant_id, file_id = contexto
    attempt_id, _ = await _registrar(factory, tenant_id, file_id)

    async with factory() as s:
        primero = await reclamar_intento(s, attempt_id)
        await s.commit()
    async with factory() as s:
        segundo = await reclamar_intento(s, attempt_id)
        await s.commit()

    assert primero is not None, "la primera entrega tiene que poder ejecutar"
    assert segundo is None, "la segunda entrega no puede volver a ejecutar"


async def test_dos_ejecutores_simultaneos_solo_uno_gana_el_token(
    contexto: tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID],
) -> None:
    factory, tenant_id, file_id = contexto
    attempt_id, _ = await _registrar(factory, tenant_id, file_id)

    async def _intentar() -> uuid.UUID | None:
        async with factory() as s:
            token = await reclamar_intento(s, attempt_id)
            await s.commit()
            return token

    tokens = await asyncio.gather(_intentar(), _intentar())
    ganadores = [t for t in tokens if t is not None]
    assert len(ganadores) == 1, f"{len(ganadores)} ejecutores se llevaron el token"


async def test_un_ejecutor_vencido_no_puede_cerrar_por_encima_de_su_reemplazo(
    contexto: tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID],
) -> None:
    """El zombi: estuvo pausado, perdió el lease, y vuelve a la vida queriendo
    publicar. Sin el token en el WHERE pisaría el resultado del que lo reemplazó,
    y los dos resultados se ven igual de válidos desde afuera."""
    factory, tenant_id, file_id = contexto
    attempt_id, _ = await _registrar(factory, tenant_id, file_id)

    async with factory() as s:
        token_viejo = await reclamar_intento(s, attempt_id)
        await s.commit()
    assert token_viejo is not None

    # Su lease vence (el reloj es el de Postgres, así que se fuerza por SQL).
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
        token_nuevo = await reclamar_intento(s, attempt_id)
        await s.commit()
    assert token_nuevo is not None and token_nuevo != token_viejo

    async with factory() as s:
        cerro_el_zombi = await cerrar_intento(
            s, attempt_id, token_viejo, estado=COMPLETADO, result={"ventas": 999}
        )
        await s.commit()
    assert cerro_el_zombi is False, "el ejecutor vencido cerró el intento de otro"

    async with factory() as s:
        cerro_el_nuevo = await cerrar_intento(
            s, attempt_id, token_nuevo, estado=COMPLETADO, result={"ventas": 3}
        )
        await s.commit()
        intento = await obtener_intento(s, tenant_id, attempt_id)
    assert cerro_el_nuevo is True
    assert intento is not None and intento.result_json == {"ventas": 3}


async def test_renovar_el_lease_con_un_token_ajeno_no_hace_nada(
    contexto: tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID],
) -> None:
    """El progreso viaja con la renovación a propósito: un ejecutor que perdió el
    lease tampoco puede seguir publicando avance de un trabajo que no es suyo."""
    factory, tenant_id, file_id = contexto
    attempt_id, _ = await _registrar(factory, tenant_id, file_id)
    async with factory() as s:
        await reclamar_intento(s, attempt_id)
        await s.commit()

    async with factory() as s:
        ok = await renovar_lease(s, attempt_id, uuid.uuid4(), phase="importando", rows_done=50)
        await s.commit()
        intento = await obtener_intento(s, tenant_id, attempt_id)
    assert ok is False
    assert intento is not None and intento.rows_done == 0


# ── Recuperación de huérfanos ────────────────────────────────────────────────
async def test_un_intento_cuyo_ejecutor_murio_vuelve_a_la_cola(
    contexto: tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID],
) -> None:
    factory, tenant_id, file_id = contexto
    attempt_id, _ = await _registrar(factory, tenant_id, file_id)
    async with factory() as s:
        await reclamar_intento(s, attempt_id)
        await s.commit()
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
        liberados = await liberar_huerfanos(s)
        await s.commit()
        intento = await obtener_intento(s, tenant_id, attempt_id)
    assert attempt_id in liberados
    assert intento is not None and intento.status == PENDIENTE


async def test_un_intento_que_agoto_sus_reintentos_no_vuelve_a_la_cola(
    contexto: tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID],
) -> None:
    """Reintentar sin límite un trabajo que mata al ejecutor sólo logra matar a
    los siguientes."""
    factory, tenant_id, file_id = contexto
    attempt_id, _ = await _registrar(factory, tenant_id, file_id)
    async with factory() as s:
        await s.execute(
            text(
                "UPDATE import_attempts SET status = :st, attempts = max_attempts, "
                "lease_token = :tok, lease_expires_at = now() - interval '1 hour' "
                "WHERE id = :id"
            ),
            {"id": attempt_id, "st": EJECUTANDO, "tok": str(uuid.uuid4())},
        )
        await s.commit()

    async with factory() as s:
        liberados = await liberar_huerfanos(s)
        await s.commit()
        intento = await obtener_intento(s, tenant_id, attempt_id)
    assert attempt_id not in liberados
    assert intento is not None and intento.status == FALLADO
    assert intento.error_detail and "reintent" in intento.error_detail


# ── Revisión externa (2026-09-10): fencing del recuperador ───────────────────
async def test_el_recuperador_no_pisa_a_un_ejecutor_que_renovo_su_lease(
    contexto: tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID],
) -> None:
    """El recuperador elige candidatos con un SELECT y escribe después.

    Entre esos dos pasos el ejecutor que parecía muerto puede **renovar** su lease:
    no estaba muerto, estaba tardando. Con la mutación del objeto ORM —SELECT y
    después ``intento.status = PENDIENTE``, sin condición en la escritura— ese
    estado posterior se pisaba y el intento volvía a la cola mientras su ejecutor
    seguía importando: dos ejecuciones del mismo trabajo, que es exactamente lo
    que el lease existe para impedir.

    Se fuerza el orden con una transacción abierta: el recuperador ya eligió, y la
    renovación entra antes de que escriba.
    """
    factory, tenant_id, file_id = contexto
    attempt_id, _ = await _registrar(factory, tenant_id, file_id, clave="fencing-1")

    async with factory() as s:
        token = await reclamar_intento(s, attempt_id)
        await s.commit()
    assert token is not None

    # El lease vence: para el recuperador es un huérfano.
    async with factory() as s:
        await s.execute(
            text(
                "UPDATE import_attempts SET lease_expires_at = now() - interval "
                "'1 hour' WHERE id = :id"
            ),
            {"id": attempt_id},
        )
        await s.commit()

    # El ejecutor renueva (no estaba muerto) y eso commitea ANTES de que el
    # recuperador escriba.
    async with factory() as s:
        assert await renovar_lease(s, attempt_id, token) is True
        await s.commit()

    async with factory() as s:
        liberados = await liberar_huerfanos(s)
        await s.commit()

    assert liberados == [], "el recuperador reencoló un intento con el lease vivo"
    async with factory() as s:
        intento = await obtener_intento(s, tenant_id, attempt_id)
        assert intento is not None
        assert intento.status == EJECUTANDO
        assert intento.lease_token == token, "le borró el token a un ejecutor vivo"


async def test_el_recuperador_no_pisa_el_resultado_de_un_intento_que_termino(
    contexto: tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID],
) -> None:
    """La otra mitad, y la peor: un ``COMPLETADO`` vuelto a ``PENDIENTE``.

    Se re-ejecuta un trabajo que ya terminó y se pierde su resultado — el usuario
    ve "importando" sobre datos que ya están, y el `result_json` que tenía la
    respuesta desaparece.
    """
    factory, tenant_id, file_id = contexto
    attempt_id, _ = await _registrar(factory, tenant_id, file_id, clave="fencing-2")

    async with factory() as s:
        token = await reclamar_intento(s, attempt_id)
        await s.commit()
    assert token is not None

    async with factory() as s:
        await s.execute(
            text(
                "UPDATE import_attempts SET lease_expires_at = now() - interval "
                "'1 hour' WHERE id = :id"
            ),
            {"id": attempt_id},
        )
        await s.commit()

    # Terminó bien, con su resultado, después de que el lease venciera.
    async with factory() as s:
        assert await cerrar_intento(
            s, attempt_id, token, estado=COMPLETADO, result={"gastos": 3}
        )
        await s.commit()

    async with factory() as s:
        assert await liberar_huerfanos(s) == []
        await s.commit()

    async with factory() as s:
        intento = await obtener_intento(s, tenant_id, attempt_id)
        assert intento is not None
        assert intento.status == COMPLETADO
        assert intento.result_json == {"gastos": 3}


async def test_un_huerfano_sin_token_igual_se_libera(
    contexto: tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID],
) -> None:
    """``EJECUTANDO`` sin token es un estado corrupto, y no puede ser eterno.

    El fencing compara el token, y en SQL ``lease_token = NULL`` no matchea NUNCA:
    sin tratar el nulo aparte, ese intento no se podría liberar jamás y el usuario
    vería "importando" para siempre. Mismo criterio que el takeover del lease de
    archivo con ``import_started_at IS NULL``.
    """
    factory, tenant_id, file_id = contexto
    attempt_id, _ = await _registrar(factory, tenant_id, file_id, clave="fencing-3")

    async with factory() as s:
        await s.execute(
            text(
                "UPDATE import_attempts SET status = 'EJECUTANDO', lease_token = NULL, "
                "lease_expires_at = now() - interval '1 hour' WHERE id = :id"
            ),
            {"id": attempt_id},
        )
        await s.commit()

    async with factory() as s:
        assert await liberar_huerfanos(s) == [attempt_id]
        await s.commit()

    async with factory() as s:
        intento = await obtener_intento(s, tenant_id, attempt_id)
        assert intento is not None
        assert intento.status == PENDIENTE


async def test_el_recuperador_deja_una_orden_pendiente_por_cada_liberado(
    contexto: tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID],
) -> None:
    """Un ``PENDIENTE`` sin orden pendiente es un intento que nadie va a ejecutar.

    La orden se re-arma en la MISMA transacción que el cambio de estado, así que
    no puede quedar una sin la otra — antes se resolvía llamando al broker después
    de commitear, y un broker caído dejaba el intento colgado para siempre.
    """
    factory, tenant_id, file_id = contexto
    attempt_id, _ = await _registrar(factory, tenant_id, file_id, clave="fencing-4")

    async with factory() as s:
        token = await reclamar_intento(s, attempt_id)
        await s.commit()
    assert token is not None

    # La orden original ya se entregó.
    async with factory() as s:
        await s.execute(
            text("UPDATE import_outbox SET published_at = now() WHERE attempt_id = :id"),
            {"id": attempt_id},
        )
        await s.execute(
            text(
                "UPDATE import_attempts SET lease_expires_at = now() - interval "
                "'1 hour' WHERE id = :id"
            ),
            {"id": attempt_id},
        )
        await s.commit()

    async with factory() as s:
        assert await liberar_huerfanos(s) == [attempt_id]
        await s.commit()

    async with factory() as s:
        pendientes = await ordenes_pendientes(s)
        assert any(o.attempt_id == attempt_id for o in pendientes), (
            "quedó un PENDIENTE sin orden pendiente: nadie lo va a ejecutar"
        )


async def test_dos_recuperadores_simultaneos_liberan_el_huerfano_una_sola_vez(
    contexto: tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID],
) -> None:
    """La carrera real del recuperador, que las pruebas de arriba NO ejercitan.

    Aquéllas cambian el estado **antes** de llamar a `liberar_huerfanos`, así que
    lo que prueban es el filtro del SELECT: se verificó por mutación que pasan
    igual con la escritura sin condiciones. La carrera que importa es la otra —el
    SELECT ya eligió y el estado cambia **después**— y se reproduce con dos
    recuperadores concurrentes sobre el mismo huérfano.

    Con la mutación del objeto ORM los dos escriben (last write wins) y los dos lo
    reportan liberado: dos órdenes para el mismo trabajo, que es la doble ejecución
    que el lease existe para impedir. Con el UPDATE condicionado, el segundo ve
    ``rowcount == 0`` y no lo cuenta.
    """
    factory, tenant_id, file_id = contexto
    attempt_id, _ = await _registrar(factory, tenant_id, file_id, clave="carrera-1")

    async with factory() as s:
        token = await reclamar_intento(s, attempt_id)
        await s.commit()
    assert token is not None

    async with factory() as s:
        await s.execute(
            text(
                "UPDATE import_attempts SET lease_expires_at = now() - interval "
                "'1 hour' WHERE id = :id"
            ),
            {"id": attempt_id},
        )
        await s.commit()

    async def _recuperar() -> list[uuid.UUID]:
        async with factory() as s:
            liberados = await liberar_huerfanos(s)
            await s.commit()
            return liberados

    a, b = await asyncio.gather(_recuperar(), _recuperar())
    veces = [attempt_id in a, attempt_id in b].count(True)
    assert veces == 1, (
        f"{veces} recuperadores liberaron el mismo huérfano: se encola dos veces el "
        "mismo trabajo"
    )
