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

Los efectos y el resultado commitean juntos
-------------------------------------------
``confirm_file`` **no** commitea: abre un savepoint y lo cierra el caller. Así que
el cierre del intento —``result_json`` + ``COMPLETADO``— va en la MISMA transacción
que los efectos, antes del único ``commit``. Una caída en el medio no existe: o
commitearon las dos cosas o ninguna.

Esto corrige algo que este módulo afirmaba y era falso. Antes el cierre iba en una
sesión aparte **después** del commit, con la ventana declarada como "se recupera
sola". No se recuperaba: lo probado era que la segunda corrida no DUPLICA (las
huellas de fila hacen el import idempotente), pero el confirm rechaza un archivo
que ya está en ``DONE``, así que el intento terminaba ``FALLADO`` informando
fracaso sobre una importación que había funcionado — y su resultado real no se
podía recuperar de ningún lado.

El mismo cambio cierra la otra mitad: si el CAS del cierre no encuentra el lease
—nos reemplazaron mientras importábamos— el ``rollback`` se lleva también los
efectos, en vez de dejarlos escritos sin dueño.
"""

from __future__ import annotations

import asyncio
import uuid as _uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.jobs.celery_app import celery_app
from app.observability.logger import get_logger

if TYPE_CHECKING:
    pass

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
        revision_ingestion = intento.ingestion_version
        revision_preview = intento.preview_version

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
                # La revisión congelada viaja acá y NO en el payload: así la huella
                # de la petición no cambia de forma (ver `_payload` en el registro).
                # `confirm_file` la verifica con el lease YA tomado, que es lo único
                # que impide que una relectura se meta en el medio.
                body=ConfirmIngestionRequest(
                    **payload,
                    revision_ingestion=revision_ingestion,
                    revision_preview=revision_preview,
                ),
                background_tasks=BackgroundTasks(),
                tenant=tenant,
                session=session,
            )
            # E7-review #3: el cierre del intento va en la MISMA transacción que
            # los efectos, ANTES del commit.
            #
            # `confirm_file` no commitea (su savepoint lo cierra el caller, que es
            # esta línea), así que se puede. Antes el cierre iba en una sesión
            # aparte después del commit, y una caída en el medio dejaba los efectos
            # escritos con el intento en EJECUTANDO. La recuperación lo reencolaba,
            # el confirm rechazaba el archivo —ya está en DONE— y el intento
            # terminaba FALLADO: **no duplicaba, pero informaba fracaso sobre una
            # importación que había funcionado**, y el resultado real no se podía
            # recuperar de ningún lado.
            #
            # Y cierra de paso la otra mitad: si el CAS no encuentra el lease
            # —nos reemplazaron mientras importábamos— el `rollback` se lleva los
            # efectos, en vez de dejarlos escritos sin dueño.
            cerrado = await cerrar_intento(
                session,
                attempt_id,
                token,
                estado=COMPLETADO,
                result=respuesta.model_dump(mode="json"),
            )
            if not cerrado:
                await session.rollback()
                # No se compensa el lease del archivo acá: perder el lease del
                # INTENTO significa que otro ejecutor nos reemplazó, y el del
                # archivo probablemente sea suyo. `release_import_lease` igual no
                # lo tocaría (compara el token), pero ni preguntarlo: el dueño del
                # estado es el que está corriendo.
                logger.warning(
                    "ingestion.intento.cierre_sin_lease", attempt_id=str(attempt_id)
                )
                return "lease_perdido"
            await session.commit()
        except LimiteExcedidoError as limite:
            await session.rollback()
            await _compensar_lease_del_archivo(factory, tenant_id, file_id)
            return await _cerrar_con_error(
                factory, attempt_id, token, ERROR_LIMITE, limite.mensaje
            )
        except Exception as exc:  # noqa: BLE001 — el detalle va al intento
            await session.rollback()
            await _compensar_lease_del_archivo(factory, tenant_id, file_id)
            codigo, detalle = _clasificar(exc)
            logger.warning(
                "ingestion.intento.fallo",
                attempt_id=str(attempt_id),
                error_code=codigo,
                error=str(exc),
            )
            return await _cerrar_con_error(factory, attempt_id, token, codigo, detalle)

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

    from app.api.v1.ingestion import REVISION_CAMBIADA_CODE  # noqa: PLC0415
    from app.application.services.ingestion_lease_service import (  # noqa: PLC0415
        ImportLeaseLostError,
    )
    from app.domain.import_attempt import (  # noqa: PLC0415
        ERROR_DESCONOCIDO,
        ERROR_IMPORT_VACIO,
        ERROR_LEASE_PERDIDO,
        ERROR_REVISION,
        ERROR_TRANSITORIO,
        ERROR_VALIDACION,
    )

    if isinstance(exc, ImportLeaseLostError):
        return ERROR_LEASE_PERDIDO, (
            "Otra importación del mismo archivo tomó el control. Consultá el "
            "estado del archivo antes de volver a intentar."
        )
    if isinstance(exc, HTTPException):
        # El 409 de revisión cambiada se distingue por su CÓDIGO, no por el texto:
        # un `ERROR_VALIDACION` diría "el archivo no valida" sobre un archivo que
        # está perfecto, y lo que cambió es su lectura.
        if (
            isinstance(exc.detail, dict)
            and exc.detail.get("code") == REVISION_CAMBIADA_CODE
        ):
            return ERROR_REVISION, str(exc.detail.get("message") or "")
        detalle = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        if exc.status_code == 422 and "no se importó" in detalle.lower():
            return ERROR_IMPORT_VACIO, detalle
        return ERROR_VALIDACION, detalle
    from sqlalchemy.exc import DBAPIError  # noqa: PLC0415

    if isinstance(exc, DBAPIError) and getattr(exc, "connection_invalidated", False):
        return ERROR_TRANSITORIO, "La base de datos se cortó durante la importación."
    return ERROR_DESCONOCIDO, str(exc)[:1000]


async def _compensar_lease_del_archivo(
    factory: Any, tenant_id: _uuid.UUID, file_id: _uuid.UUID
) -> None:
    """Devuelve el archivo a NEEDS_CONFIRMATION cuando el import se revirtió.

    ``acquire_import_lease`` **commitea** el ``IMPORTING`` a propósito, para que un
    confirm concurrente lo vea antes de que arranque el import largo. Eso significa
    que un fallo POSTERIOR al retorno de ``confirm_file`` —el cierre del intento, por
    ejemplo— no lo deshace con un ``rollback``: el propio manejador de
    ``confirm_file`` no corre, porque la excepción pasó afuera. El archivo quedaba
    trabado en "importando" hasta que venciera el TTL, con sus efectos revertidos:
    el estado decía una cosa y los libros otra.

    El token se lee del archivo para no pisar a un takeover: si otro ejecutor ya lo
    tomó, ``release_import_lease`` no encuentra su token y no hace nada.
    """
    from app.application.services.ingestion_lease_service import (  # noqa: PLC0415
        release_import_lease,
    )
    from app.persistence.models.file import UploadedFile  # noqa: PLC0415

    # Sesión PROPIA y no la que acaba de fallar: ésa ya pasó por un rollback en
    # medio de una excepción y no es un lugar del que se pueda depender para una
    # compensación. Mismo criterio que `_cerrar_con_error`.
    async with factory() as session:
        token = (
            await session.execute(
                select(UploadedFile.import_attempt_id).where(
                    UploadedFile.id == file_id, UploadedFile.tenant_id == tenant_id
                )
            )
        ).scalar_one_or_none()
        if token is None:
            return
        await release_import_lease(session, tenant_id, file_id, token)


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
        # `liberar_huerfanos` re-arma la orden de cada liberado en ESTA transacción.
        # La recuperación NO llama al broker: si lo hiciera y la llamada fallara
        # —broker caído, que es un motivo habitual de que haya huérfanos— el intento
        # quedaría PENDIENTE sin orden pendiente, invisible para el publicador y sin
        # ejecución para siempre. El commit es lo único que decide, igual que en el
        # registro; la entrega es del publicador, que es reintentable.
        liberados = await liberar_huerfanos(session)
        await session.commit()
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
