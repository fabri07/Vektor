"""Ejecutar un intento de importación, publicar sus órdenes y recuperar huérfanos.

Las tres tasks y por qué son tres
----------------------------------
``jobs.publish_import_orders`` lee ``import_outbox`` y entrega al broker lo que
todavía no salió. Corre periódicamente **y** se dispara después de cada confirm:
lo primero garantiza que nada quede sin entregar, lo segundo hace que el caso
normal no espere al siguiente tick.

``jobs.execute_import`` reclama un intento y lo ejecuta.

``jobs.recover_import_attempts`` devuelve a la cola los intentos cuyo ejecutor
murió. Sin esto, un worker que se cae deja el trabajo en ``EJECUTANDO`` para
siempre y el usuario ve "importando" sin que nadie esté importando.

La decisión que gobierna todo el módulo
----------------------------------------
**El ejecutor llama a ``confirm_file``, el mismo endpoint que usa la ruta
sincrónica.** No reimplementa el import.

No es comodidad: es la única forma de que las dos rutas compartan la protección
por archivo. ``confirm_file`` toma el lease de ``uploaded_files``
(``acquire_import_lease`` → CAS atómico), lo verifica al publicar
(``finalize_import_lease``, que ante token perdido lanza y hace **rollback de las
escrituras de negocio**, no sólo del cierre del intento) y lo compensa ante error.
Si el ejecutor tuviera su propio camino, habría dos exclusiones que alguien tiene
que acordarse de mantener sincronizadas, y `/confirm` podría importar el mismo
archivo mientras el ejecutor lo importa.

Que se pueda llamar como función y no por HTTP ya estaba probado: es lo que hace
``scripts/bench_confirm_import.py`` para medir el endpoint entero.

La ventana que queda, dicha en voz alta
----------------------------------------
Los efectos del import y el cierre del intento **no** se commitean juntos:
``confirm_file`` cierra su propia transacción. Si el proceso muere entre el commit
de los efectos y el cierre del intento, el intento queda ``EJECUTANDO`` con el
lease vencido y la recuperación lo vuelve a encolar. La segunda corrida no
duplica nada —las huellas de fila hacen el import idempotente: reimportar el mismo
archivo inserta cero filas— y cierra el intento con el resultado correcto. O sea:
la ventana existe, se recupera sola, y el precio es una corrida extra que no
escribe nada.
"""

from __future__ import annotations

import asyncio
import uuid as _uuid
from typing import Any

from app.jobs.celery_app import celery_app
from app.observability.logger import get_logger

logger = get_logger(__name__)

_QUEUE = "ingestion"


def _run(coro: Any) -> Any:
    """Corre una corrutina en el worker sincrónico de Celery."""
    return asyncio.run(coro)


def _sesion_de_worker() -> tuple[Any, Any]:
    """Engine y sessionmaker propios del worker.

    Un worker de Celery no comparte el engine del proceso web: corre en otro
    proceso, con otro loop. Es el mismo patrón que ``ingestion_worker`` — se
    reusa su helper para no tener dos formas de abrir la conexión que puedan
    divergir en pool, ``pre_ping`` o ``connect_args``.
    """
    from app.config.settings import get_settings  # noqa: PLC0415
    from app.jobs.ingestion_worker import _build_async_session  # noqa: PLC0415

    engine, factory = _build_async_session(get_settings().DATABASE_URL)
    return factory, engine


# ── Publicador ───────────────────────────────────────────────────────────────
async def _publicar_ordenes(limite: int = 100) -> int:
    from app.application.services.import_attempt_service import (
        marcar_publicada,
        ordenes_pendientes,
    )
    factory, engine = _sesion_de_worker()
    publicadas = 0
    async with factory() as session:
        pendientes = await ordenes_pendientes(session, limite=limite)
        for orden in pendientes:
            try:
                # PRIMERO entregar, DESPUÉS marcar. Al revés, morir entre los dos
                # pasos perdería la orden para siempre: quedaría marcada como
                # publicada sin que nadie la haya recibido. En este orden, morir en
                # el medio produce una entrega repetida — que el lease del ejecutor
                # ya sabe descartar.
                celery_app.send_task(
                    "jobs.execute_import",
                    args=[str(orden.attempt_id)],
                    queue=_QUEUE,
                )
            except Exception as exc:  # noqa: BLE001 — un broker caído no es un bug
                orden.publish_attempts += 1
                orden.last_error = str(exc)[:1000]
                logger.warning(
                    "ingestion.outbox.publicacion_fallida",
                    attempt_id=str(orden.attempt_id),
                    intentos=orden.publish_attempts,
                    error=str(exc),
                )
                continue
            await marcar_publicada(session, orden.id)
            publicadas += 1
        await session.commit()
    if publicadas:
        logger.info("ingestion.outbox.publicadas", cantidad=publicadas)
    return publicadas


@celery_app.task(name="jobs.publish_import_orders", queue=_QUEUE)  # type: ignore[misc]
def publish_import_orders() -> dict[str, int]:
    """Entrega al broker las órdenes pendientes. Idempotente y reintentable."""
    return {"publicadas": int(_run(_publicar_ordenes()))}


# ── Ejecutor ─────────────────────────────────────────────────────────────────
async def _ejecutar(attempt_id: _uuid.UUID) -> str:
    from app.api.v1.ingestion import confirm_file
    from app.application.services.import_attempt_service import (
        cerrar_intento,
        reclamar_intento,
    )
    from app.domain.import_attempt import (
        COMPLETADO,
        ERROR_ARCHIVO_BORRADO,
        ERROR_CAPACIDADES,
        ERROR_DESCONOCIDO,
        ERROR_LIMITE,
        ERROR_VALIDACION,
        FALLADO,
    )
    from app.domain.ingestion_limits import LimiteExcedidoError
    from app.persistence.models.import_attempt import ImportAttempt
    from app.persistence.models.tenant import Tenant
    from app.schemas.ingestion import ConfirmIngestionRequest

    factory, engine = _sesion_de_worker()

    # 1. Reclamar. Si no se puede, esta entrega es un duplicado y se va.
    async with factory() as session:
        token = await reclamar_intento(session, attempt_id)
        await session.commit()
    if token is None:
        return "duplicado"

    async with factory() as session:
        intento = await session.get(ImportAttempt, attempt_id)
        if intento is None:  # pragma: no cover — lo acabamos de reclamar
            return "inexistente"
        payload = dict(intento.payload_json or {})
        tenant_id = intento.tenant_id
        file_id = intento.file_id
        payload_version = intento.payload_version
        hash_congelado = intento.file_content_hash
        capacidades_congeladas = intento.capabilities_json

    from app.application.services.import_attempt_service import (  # noqa: PLC0415
        PAYLOAD_VERSION,
    )

    if payload_version != PAYLOAD_VERSION:
        # Un intento guardado con otro formato de sobre. Interpretarlo "lo mejor
        # posible" es lo peligroso: los campos que cambiaron de significado se
        # leerían mal en silencio.
        return await _cerrar_con_error(
            factory,
            attempt_id,
            token,
            ERROR_VALIDACION,
            "Esta importación se registró con una versión anterior del sistema. "
            "Volvé a confirmar el archivo.",
        )

    # E7a-lite: ¿las compuertas de rollout siguen siendo las mismas que cuando el
    # usuario confirmó? Este proceso NO es el que armó el preview —la api y el
    # worker son dos servicios con entornos propios que Railway redespliega en
    # paralelo— así que importar sin mirar esto puede guardar números distintos de
    # los que mostró la pantalla, sin un solo error a la vista. Va ANTES de tocar
    # nada: el valor de verificar es no haber escrito.
    from app.domain.import_capabilities import (  # noqa: PLC0415
        capacidades_efectivas,
        diferencias,
        texto_de_diferencias,
    )

    _difs = diferencias(capacidades_congeladas, capacidades_efectivas(tenant_id))
    if _difs:
        logger.warning(
            "ingestion.intento.capacidades_cambiaron",
            attempt_id=str(attempt_id),
            tenant_id=str(tenant_id),
            diferencias={k: list(v) for k, v in _difs.items()},
        )
        return await _cerrar_con_error(
            factory,
            attempt_id,
            token,
            ERROR_CAPACIDADES,
            texto_de_diferencias(_difs),
        )

    # 2. Ejecutar, con el MISMO endpoint que usa la ruta sincrónica.
    async with factory() as session:
        tenant = await session.get(Tenant, tenant_id)
        archivo = await _archivo_vigente(session, tenant_id, file_id, hash_congelado)
        if tenant is None or archivo is None:
            return await _cerrar_con_error(
                factory,
                attempt_id,
                token,
                ERROR_ARCHIVO_BORRADO,
                "El archivo ya no está disponible, o cambió desde que confirmaste. "
                "Volvé a subirlo y confirmalo de nuevo.",
            )
        try:
            from fastapi import BackgroundTasks  # noqa: PLC0415

            respuesta = await confirm_file(
                file_id=file_id,
                body=ConfirmIngestionRequest(**payload),
                background_tasks=BackgroundTasks(),
                tenant=tenant,
                session=session,
            )
            await session.commit()
        except LimiteExcedidoError as limite:
            await session.rollback()
            return await _cerrar_con_error(
                factory, attempt_id, token, ERROR_LIMITE, limite.mensaje
            )
        except Exception as exc:  # noqa: BLE001 — el detalle va al intento
            await session.rollback()
            codigo, detalle = _clasificar(exc)
            logger.warning(
                "ingestion.intento.fallo",
                attempt_id=str(attempt_id),
                error_code=codigo,
                error=str(exc),
            )
            return await _cerrar_con_error(factory, attempt_id, token, codigo, detalle)

    # 3. Cerrar con el resultado. Ver la nota del encabezado sobre la ventana
    #    entre el commit de los efectos y este cierre.
    async with factory() as session:
        cerrado = await cerrar_intento(
            session,
            attempt_id,
            token,
            estado=COMPLETADO,
            result=respuesta.model_dump(mode="json"),
        )
        await session.commit()
    if not cerrado:
        # Perdimos el lease mientras importábamos. Los efectos ya se escribieron
        # (o los revirtió `finalize_import_lease`, que es quien decide eso); lo
        # que no podemos es pisar el estado de quien nos reemplazó.
        logger.warning("ingestion.intento.cierre_sin_lease", attempt_id=str(attempt_id))
        return "lease_perdido"
    _ = FALLADO, ERROR_DESCONOCIDO  # referenciados por `_clasificar`
    return "completado"


async def _archivo_vigente(
    session: Any, tenant_id: _uuid.UUID, file_id: _uuid.UUID, hash_congelado: str | None
) -> Any:
    """El archivo, si sigue existiendo Y es la versión que se confirmó.

    Comparar el hash no es paranoia: entre el confirm y la ejecución puede haber
    corrido una relectura, que reemplaza el contenido interpretado del archivo.
    Importar contra el contenido nuevo sería importar algo que el usuario nunca
    vio en la pantalla donde dijo que sí.
    """
    from app.persistence.models.file import UploadedFile  # noqa: PLC0415

    archivo = await session.get(UploadedFile, file_id)
    if archivo is None or archivo.tenant_id != tenant_id or archivo.deleted_at is not None:
        return None
    if hash_congelado and archivo.content_hash and archivo.content_hash != hash_congelado:
        return None
    return archivo


def _clasificar(exc: BaseException) -> tuple[str, str]:
    """Error → (código estructurado, detalle accionable).

    El código decide si conviene reintentar y se puede contar; el detalle lo lee
    una persona. Devolver `str(exc)` para las dos cosas no sirve para ninguna.
    """
    from fastapi import HTTPException  # noqa: PLC0415

    from app.application.services.ingestion_lease_service import (  # noqa: PLC0415
        ImportLeaseLostError,
    )
    from app.domain.import_attempt import (  # noqa: PLC0415
        ERROR_DESCONOCIDO,
        ERROR_IMPORT_VACIO,
        ERROR_LEASE_PERDIDO,
        ERROR_TRANSITORIO,
        ERROR_VALIDACION,
    )

    if isinstance(exc, ImportLeaseLostError):
        return ERROR_LEASE_PERDIDO, (
            "Otra importación del mismo archivo tomó el control. Consultá el "
            "estado del archivo antes de volver a intentar."
        )
    if isinstance(exc, HTTPException):
        detalle = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        if exc.status_code == 422 and "no se importó" in detalle.lower():
            return ERROR_IMPORT_VACIO, detalle
        return ERROR_VALIDACION, detalle
    from sqlalchemy.exc import DBAPIError  # noqa: PLC0415

    if isinstance(exc, DBAPIError) and getattr(exc, "connection_invalidated", False):
        return ERROR_TRANSITORIO, "La base de datos se cortó durante la importación."
    return ERROR_DESCONOCIDO, str(exc)[:1000]


async def _cerrar_con_error(
    factory: Any,
    attempt_id: _uuid.UUID,
    token: _uuid.UUID,
    codigo: str,
    detalle: str,
) -> str:
    from app.application.services.import_attempt_service import (  # noqa: PLC0415
        cerrar_intento,
    )
    from app.domain.import_attempt import FALLADO  # noqa: PLC0415

    async with factory() as session:
        await cerrar_intento(
            session,
            attempt_id,
            token,
            estado=FALLADO,
            error_code=codigo,
            error_detail=detalle,
        )
        await session.commit()
    return "fallado"


@celery_app.task(name="jobs.execute_import", queue=_QUEUE, max_retries=0)  # type: ignore[misc]
def execute_import(attempt_id: str) -> dict[str, str]:
    """Ejecuta UN intento. Tolera la entrega duplicada sin hacer nada.

    ``max_retries=0`` a propósito: los reintentos los gobierna el intento —con su
    contador, su lease y la recuperación de huérfanos—, no el broker. Dos
    mecanismos de reintento sobre el mismo trabajo se pisan y producen justo lo
    que se quiere evitar: dos ejecuciones a la vez.
    """
    return {"resultado": str(_run(_ejecutar(_uuid.UUID(attempt_id))))}


# ── Recuperación ─────────────────────────────────────────────────────────────
async def _recuperar() -> int:
    from app.application.services.import_attempt_service import liberar_huerfanos

    factory, engine = _sesion_de_worker()
    async with factory() as session:
        liberados = await liberar_huerfanos(session)
        await session.commit()
    # Los reencolados necesitan una orden nueva: la original ya se marcó publicada.
    for attempt_id in liberados:
        celery_app.send_task(
            "jobs.execute_import", args=[str(attempt_id)], queue=_QUEUE
        )
    return len(liberados)


@celery_app.task(name="jobs.recover_import_attempts", queue="scores")  # type: ignore[misc]
def recover_import_attempts() -> dict[str, int]:
    """Devuelve a la cola los intentos cuyo ejecutor murió.

    Vive en la cola ``scores`` y no en ``ingestion`` por el mismo motivo que el
    barrido de relecturas: es el AUDITOR de la cola de ingestión, y si viviera en
    ella se trabaría con lo que audita — que es exactamente el incidente que este
    programa vino a cerrar.
    """
    return {"reencolados": int(_run(_recuperar()))}
