"""Registrar, reclamar y cerrar un intento de importación.

Las tres operaciones y qué garantiza cada una
----------------------------------------------
``registrar_intento`` escribe el intento **y su orden de ejecución en la misma
transacción**. Es lo único que cierra la ventana entre el commit y la
publicación: si commiteó, la orden existe y alguien la va a entregar; si no
commiteó, no existe ni el intento ni la orden. Y es idempotente por clave de
petición — repetir el mismo confirm por timeout devuelve el intento que ya está.

``reclamar_intento`` es el fencing del EJECUTOR, que es otra cosa que la
idempotencia de petición y se confunde seguido. La misma orden puede llegar dos
veces (el publicador murió después de enviarla y antes de marcarla): la segunda
entrega no encuentra nada que reclamar y se va. Y un ejecutor cuyo lease venció no
puede publicar por encima del que lo reemplazó.

``cerrar_intento`` aplica una transición VÁLIDA con el token en el ``WHERE``. Un
``UPDATE`` sin token deja que un ejecutor zombi pise el resultado bueno de otro,
que es exactamente el defecto que el fencing del parseo ya tuvo (E1).

Lo que este módulo NO promete
------------------------------
"Exactly once" de transporte. Celery no lo da y fingirlo sería peor que no
tenerlo: la garantía está en la adquisición (una sola ejecución gana el token) y
en la publicación atómica (los efectos se escriben con el token verificado), no en
que el mensaje llegue una sola vez.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services._savepoint import (
    SavepointConflictError,
    guarded_savepoint,
    unique_violation_classifier,
)
from app.domain.import_attempt import (
    COMPLETADO,
    EJECUTANDO,
    ERROR_TRANSITORIO,
    FALLADO,
    PENDIENTE,
    exigir_transicion,
    huella_de_solicitud,
)
from app.domain.import_capabilities import capacidades_efectivas
from app.observability.logger import get_logger
from app.persistence.models.import_attempt import ImportAttempt, ImportOutbox

logger = get_logger(__name__)

#: Cuánto vale un lease de ejecución. Más largo que el import más lento medido
#: (5.000 filas ≈ 25 s) por dos órdenes de magnitud: el lease está para detectar
#: un ejecutor MUERTO, no para apurar a uno lento. Un TTL corto produce el peor
#: de los mundos — dos ejecutores corriendo lo mismo a la vez.
LEASE_TTL_SEGUNDOS = 15 * 60

#: Versión del FORMATO del payload congelado. **Subirla cada vez que cambie la
#: forma del sobre**: un ejecutor nuevo que interpreta un sobre viejo "lo mejor
#: posible" lee mal los campos que cambiaron de significado, en silencio. Con la
#: versión, los rechaza diciendo que hay que volver a confirmar.
PAYLOAD_VERSION = 1

_CONFLICTO_CLAVE = unique_violation_classifier(
    "request_key",
    constraint="uq_import_attempts_request_key",
    columns=("import_attempts.tenant_id", "import_attempts.request_key"),
)


def _vence_en(session: AsyncSession, segundos: int) -> Any:
    """``now() + segundos`` en el reloj de la BASE, por dialecto.

    El reloj tiene que ser el de la base y no el del proceso: dos workers con el
    reloj corrido decidirían distinto sobre el mismo lease.

    Y la aritmética va por dialecto porque no es portable. En SQLite,
    ``func.now() + timedelta(...)`` renderiza una SUMA NUMÉRICA sobre un string de
    fecha y devuelve un entero — que después revienta al leerlo como datetime.
    Es el mismo motivo por el que `_stale_before_expr` del lease de archivo
    existe; se sigue su forma para no tener dos maneras de escribir lo mismo.
    """
    bind = session.bind
    dialecto = bind.dialect.name if bind is not None else ""
    if dialecto == "postgresql":
        # make_interval(años, meses, semanas, días, horas, mins, segs) — POSICIONAL:
        # los kwargs a `func.*` no se traducen a args SQL con nombre.
        return func.now() + func.make_interval(0, 0, 0, 0, 0, 0, segundos)
    return func.datetime(func.now(), f"+{int(segundos)} seconds")


@dataclass(frozen=True)
class ResultadoDeRegistro:
    """Qué pasó al registrar. ``creado=False`` significa "ya existía"."""

    intento: ImportAttempt
    creado: bool


class SolicitudEnConflictoError(Exception):
    """La misma clave de petición para OTRA petición: otro archivo, u otro contenido.

    No se puede resolver sola y por eso es un error y no una decisión: devolver el
    intento viejo importaría algo que el usuario no pidió, y crear uno nuevo
    rompería la promesa de la clave. Se le dice al cliente que use otra clave o
    que consulte el intento que ya existe.
    """

    def __init__(self, intento_existente: ImportAttempt) -> None:
        self.intento_existente = intento_existente
        super().__init__(
            "Ya hay una importación registrada con esta misma clave de petición "
            "pero para otro archivo o con otro contenido."
        )


async def registrar_intento(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    file_id: uuid.UUID,
    request_key: str,
    payload: dict[str, Any],
    ingestion_version: int = 1,
    preview_version: int | None = None,
    rows_total: int | None = None,
    file_content_hash: str | None = None,
) -> ResultadoDeRegistro:
    """Registra la intención y su orden de ejecución, en la misma transacción.

    **No commitea.** El caller decide cuándo, y ése es el punto: el commit es lo
    único que hace existir a las dos cosas juntas.

    Idempotente por ``(tenant_id, request_key)``:

    * misma clave y mismo contenido → devuelve el intento existente
      (``creado=False``), sin tocar nada;
    * misma clave y contenido distinto → ``SolicitudEnConflictoError``;
    * clave nueva → crea intento + orden.

    La carrera entre dos requests idénticos la resuelve el UNIQUE, no un SELECT
    previo: con un ``if not existe: crear`` los dos pasarían el chequeo y el
    segundo reventaría con IntegrityError en el commit, cuando ya es tarde para
    responder algo útil.
    """
    huella = huella_de_solicitud(payload)

    existente = await _buscar_por_clave(session, tenant_id, request_key)
    if existente is not None:
        return _resolver_existente(existente, huella, file_id)

    intento = ImportAttempt(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        file_id=file_id,
        request_key=request_key,
        payload_hash=huella,
        payload_json=payload,
        payload_version=PAYLOAD_VERSION,
        # E7a-lite: qué compuertas de rollout estaban efectivas cuando el usuario
        # dijo que sí. Se mide ACÁ —en el proceso que atiende el confirm, que es el
        # mismo que armó el preview— porque el que ejecuta puede tener otro
        # entorno. El ejecutor verifica antes de escribir.
        capabilities_json=capacidades_efectivas(tenant_id),
        ingestion_version=ingestion_version,
        preview_version=preview_version,
        file_content_hash=file_content_hash,
        rows_total=rows_total,
        status=PENDIENTE,
    )
    try:
        async with guarded_savepoint(session, _CONFLICTO_CLAVE):
            session.add(intento)
    except SavepointConflictError:
        # Otro request con la misma clave ganó la carrera entre nuestro SELECT y
        # nuestro INSERT. Se relee y se responde igual que si hubiera estado desde
        # el principio — para el cliente es el mismo caso.
        ganador = await _buscar_por_clave(session, tenant_id, request_key)
        if ganador is None:  # pragma: no cover — el unique acaba de rechazarnos
            raise
        return _resolver_existente(ganador, huella, file_id)

    # La ORDEN, en esta misma transacción. Si el caller revierte, se van las dos.
    session.add(
        ImportOutbox(id=uuid.uuid4(), attempt_id=intento.id, tenant_id=tenant_id)
    )
    logger.info(
        "ingestion.intento.registrado",
        tenant_id=str(tenant_id),
        file_id=str(file_id),
        attempt_id=str(intento.id),
    )
    return ResultadoDeRegistro(intento, creado=True)


def _resolver_existente(
    intento: ImportAttempt, huella: str, file_id: uuid.UUID
) -> ResultadoDeRegistro:
    """¿La petición que ya existe es LA MISMA petición?

    El ``file_id`` entra en la comparación y no en la huella del payload, por dos
    razones. La primera es que tiene que entrar: el archivo no está en el cuerpo
    —es un parámetro de la ruta— así que dos archivos distintos con los mismos
    mapeos dan la misma huella, y sin esto el segundo recibía el intento del
    PRIMERO. El cliente creía haber registrado su importación y ese archivo no se
    importaba nunca: no es un duplicado, es una pérdida silenciosa.

    La segunda es por qué no va adentro del hash: cambiar la entrada de la huella
    invalidaría los intentos ya registrados —la misma clave daría otra huella y
    pasaría a ser un conflicto— y romper lo que está en vuelo es justamente lo que
    esta ruta existe para evitar.
    """
    if intento.file_id != file_id or intento.payload_hash != huella:
        raise SolicitudEnConflictoError(intento)
    return ResultadoDeRegistro(intento, creado=False)


async def _buscar_por_clave(
    session: AsyncSession, tenant_id: uuid.UUID, request_key: str
) -> ImportAttempt | None:
    return (
        await session.execute(
            select(ImportAttempt).where(
                ImportAttempt.tenant_id == tenant_id,
                ImportAttempt.request_key == request_key,
            )
        )
    ).scalar_one_or_none()


async def reclamar_intento(
    session: AsyncSession, attempt_id: uuid.UUID, *, ttl_segundos: int = LEASE_TTL_SEGUNDOS
) -> uuid.UUID | None:
    """Toma el intento para ejecutarlo. Devuelve el token, o ``None`` si no se pudo.

    ``None`` no es un error: es el caso normal de una **segunda entrega** de la
    misma orden. El ``WHERE`` exige que el intento esté ``PENDIENTE`` —o
    ``EJECUTANDO`` con el lease vencido, que es un ejecutor muerto—, así que el
    duplicado no encuentra nada que reclamar y se va sin hacer nada.

    El CAS es un solo ``UPDATE`` con ``rowcount``: atómico a nivel de fila, así que
    dos ejecutores que llegan a la vez se serializan y sólo uno se lleva el token.
    Un ``SELECT`` seguido de un ``UPDATE`` dejaría pasar a los dos.

    Reloj de PostgreSQL (``func.now()``), nunca el del proceso: dos workers con el
    reloj corrido decidirían distinto sobre el mismo lease.
    """
    token = uuid.uuid4()
    vencido = ImportAttempt.lease_expires_at < func.now()
    resultado = await session.execute(
        update(ImportAttempt)
        .where(
            ImportAttempt.id == attempt_id,
            (ImportAttempt.status == PENDIENTE)
            | ((ImportAttempt.status == EJECUTANDO) & vencido),
        )
        .values(
            status=EJECUTANDO,
            lease_token=token,
            lease_expires_at=_vence_en(session, ttl_segundos),
            started_at=func.coalesce(ImportAttempt.started_at, func.now()),
            attempts=ImportAttempt.attempts + 1,
            phase="reclamado",
        )
        .execution_options(synchronize_session=False)
    )
    if cast("CursorResult[Any]", resultado).rowcount != 1:
        logger.info("ingestion.intento.ya_reclamado", attempt_id=str(attempt_id))
        return None
    return token


async def renovar_lease(
    session: AsyncSession,
    attempt_id: uuid.UUID,
    token: uuid.UUID,
    *,
    ttl_segundos: int = LEASE_TTL_SEGUNDOS,
    phase: str | None = None,
    rows_done: int | None = None,
) -> bool:
    """Extiende el lease y publica el progreso. ``False`` = el token ya no es nuestro.

    Es también el punto donde la pantalla se entera de que algo avanza: el
    progreso viaja con la renovación en vez de en un escritura aparte, así que un
    ejecutor que perdió el lease tampoco puede seguir publicando progreso de un
    trabajo que ya no es suyo.
    """
    valores: dict[str, Any] = {"lease_expires_at": _vence_en(session, ttl_segundos)}
    if phase is not None:
        valores["phase"] = phase
    if rows_done is not None:
        valores["rows_done"] = rows_done
    resultado = await session.execute(
        update(ImportAttempt)
        .where(
            ImportAttempt.id == attempt_id,
            ImportAttempt.lease_token == token,
            ImportAttempt.status == EJECUTANDO,
        )
        .values(**valores)
        .execution_options(synchronize_session=False)
    )
    return bool(cast("CursorResult[Any]", resultado).rowcount == 1)


async def cerrar_intento(
    session: AsyncSession,
    attempt_id: uuid.UUID,
    token: uuid.UUID,
    *,
    estado: str,
    result: dict[str, Any] | None = None,
    error_code: str | None = None,
    error_detail: str | None = None,
) -> bool:
    """Cierra el intento con el token verificado. ``False`` = no era nuestro.

    El token va en el ``WHERE`` y no en una comprobación previa. Sin él, un
    ejecutor zombi —uno que estuvo pausado, perdió el lease y volvió— pisaría el
    resultado del que lo reemplazó, y los dos resultados se ven igual de válidos
    desde afuera. Es el mismo defecto que el fencing del parseo ya tuvo (E1).

    **No commitea**: el cierre tiene que ir en la misma transacción que los
    efectos del import. Si se commiteara acá, existiría un instante donde el
    intento dice COMPLETADO y las ventas todavía no están.
    """
    exigir_transicion(EJECUTANDO, estado)
    resultado = await session.execute(
        update(ImportAttempt)
        .where(
            ImportAttempt.id == attempt_id,
            ImportAttempt.lease_token == token,
            ImportAttempt.status == EJECUTANDO,
        )
        .values(
            status=estado,
            result_json=result,
            error_code=error_code,
            error_detail=(error_detail or None) and error_detail[:2000],
            finished_at=func.now(),
            phase=None,
            lease_token=None,
            lease_expires_at=None,
        )
        .execution_options(synchronize_session=False)
    )
    if cast("CursorResult[Any]", resultado).rowcount != 1:
        logger.warning(
            "ingestion.intento.cierre_sin_propiedad",
            attempt_id=str(attempt_id),
            estado=estado,
        )
        return False
    return True


async def reencolar_orden(session: AsyncSession, attempt_id: uuid.UUID) -> None:
    """Vuelve a dejar pendiente la orden de este intento. **No commitea.**

    Un intento que vuelve a ``PENDIENTE`` necesita una orden pendiente, o nadie lo
    va a ejecutar: la original ya se marcó publicada. Y tiene que quedar pendiente
    **en la misma transacción** que el cambio de estado.

    Antes la recuperación resolvía esto llamando al broker directamente después de
    commitear. Si esa llamada fallaba —broker caído, que es un motivo habitual de
    que un ejecutor muera y haya huérfanos que recuperar— el intento quedaba
    ``PENDIENTE`` sin ninguna orden pendiente: invisible para el publicador y para
    siempre sin ejecución. O sea que la recuperación reabría exactamente la ventana
    que el outbox existe para cerrar, y en el peor momento posible.

    Se re-arma la fila que ya existe en vez de insertar otra: una orden por intento,
    y el contador ``publish_attempts`` sigue contando sobre la misma — que es lo que
    distingue un broker caído de un archivo malo.
    """
    await session.execute(
        update(ImportOutbox)
        .where(ImportOutbox.attempt_id == attempt_id)
        .values(published_at=None)
        .execution_options(synchronize_session=False)
    )


async def liberar_huerfanos(
    session: AsyncSession, *, limite: int = 50
) -> list[uuid.UUID]:
    """Devuelve a PENDIENTE los intentos cuyo ejecutor murió. Los ids liberados.

    Un ``EJECUTANDO`` con el lease vencido es, por definición, un ejecutor que no
    renovó: o murió, o se colgó tanto que ya no se le puede creer. Volverlo a
    ``PENDIENTE`` **y re-armar su orden en la misma transacción** es lo que permite
    que el publicador lo entregue de nuevo sin depender de que el broker responda
    justo ahora (ver ``reencolar_orden``).

    Los que ya agotaron ``max_attempts`` se marcan ``FALLADO`` en vez de volver a
    la cola: reintentar sin límite un trabajo que mata al ejecutor lo único que
    logra es matar a los siguientes.
    """
    # El SELECT sólo elige CANDIDATOS. La decisión la toma cada UPDATE, que repite
    # las condiciones en su propio WHERE. Es el mismo patrón CAS que
    # `reclamar_intento` y `cerrar_intento` ya usan; éste era el único de los tres
    # que no lo seguía —hacía SELECT y después mutaba el objeto ORM, o sea escribía
    # sobre un estado que ya no había verificado—.
    #
    # Lo que eso rompía, medido: dos recuperadores concurrentes sobre el mismo
    # huérfano lo liberaban LOS DOS (last write wins, los dos lo reportan), o sea
    # dos órdenes para el mismo trabajo — la doble ejecución que el lease existe
    # para impedir. Con el UPDATE condicionado el segundo ve `rowcount == 0`.
    # Compuerta: `test_dos_recuperadores_simultaneos_liberan_el_huerfano_una_sola_vez`.
    candidatos = (
        (
            await session.execute(
                select(
                    ImportAttempt.id,
                    ImportAttempt.attempts,
                    ImportAttempt.max_attempts,
                )
                .where(
                    ImportAttempt.status == EJECUTANDO,
                    ImportAttempt.lease_expires_at < func.now(),
                )
                .limit(limite)
            )
        )
        .tuples()
        .all()
    )
    liberados: list[uuid.UUID] = []
    tomados = 0
    for attempt_id, intentos, maximo in candidatos:
        agotado = intentos >= maximo
        valores: dict[str, Any] = {
            "status": FALLADO if agotado else PENDIENTE,
            "lease_token": None,
            "lease_expires_at": None,
            "phase": None,
        }
        if agotado:
            valores["error_code"] = ERROR_TRANSITORIO
            valores["error_detail"] = (
                "La importación se interrumpió y ya se reintentó el máximo de "
                "veces. Volvé a confirmar el archivo."
            )
            valores["finished_at"] = datetime.now(UTC)
        resultado = await session.execute(
            update(ImportAttempt)
            .where(
                ImportAttempt.id == attempt_id,
                # Las dos condiciones que hacían huérfano a este intento, repetidas
                # acá para que la escritura no pueda aplicarse sobre otro estado.
                ImportAttempt.status == EJECUTANDO,
                ImportAttempt.lease_expires_at < func.now(),
                # NO se compara el `lease_token`.
                #
                # Parecía la pieza obvia del fencing y se probó: ningún escenario la
                # necesita. Los tres casos que preocupaban —el ejecutor renovó,
                # terminó, o lo reemplazó otro— mueven `status` o `lease_expires_at`,
                # y las dos condiciones de arriba ya los excluyen; un token viejo
                # sobre un intento que volvió a quedar huérfano describe un huérfano
                # de verdad, así que liberarlo es correcto. Una mutación que borraba
                # la comparación de token NO rompió ninguna prueba: código sin un
                # caso que lo justifique, y con un borde propio (``lease_token =
                # NULL`` no matchea NUNCA en SQL, así que un EJECUTANDO con token
                # nulo no se habría podido liberar jamás).
            )
            .values(**valores)
        )
        if cast("CursorResult[Any]", resultado).rowcount != 1:
            # El intento se movió solo: renovó, terminó, o lo tomó otro. No es un
            # error — es la prueba de que no estaba huérfano.
            logger.info(
                "ingestion.intento.huerfano_se_movio_solo", attempt_id=str(attempt_id)
            )
            continue
        tomados += 1
        if not agotado:
            # La orden se re-arma ACÁ —misma transacción que el cambio de estado—
            # para que no pueda existir un PENDIENTE sin orden pendiente.
            await reencolar_orden(session, attempt_id)
            liberados.append(attempt_id)
    if candidatos:
        logger.info(
            "ingestion.intento.huerfanos",
            candidatos=len(candidatos),
            tomados=tomados,
            reencolados=len(liberados),
        )
    return liberados


async def marcar_publicada(session: AsyncSession, outbox_id: uuid.UUID) -> None:
    """La orden ya se entregó al broker."""
    await session.execute(
        update(ImportOutbox)
        .where(ImportOutbox.id == outbox_id)
        .values(published_at=func.now())
        .execution_options(synchronize_session=False)
    )


async def marcar_publicada_del_intento(
    session: AsyncSession, attempt_id: uuid.UUID
) -> None:
    """Marca publicada la orden de ESTE intento, por su id de intento.

    El confirm publica apenas commitea —para que el caso normal no espere al
    próximo tick del publicador— y necesita marcarla sin haber leído la fila.
    """
    await session.execute(
        update(ImportOutbox)
        .where(ImportOutbox.attempt_id == attempt_id, ImportOutbox.published_at.is_(None))
        .values(published_at=func.now())
        .execution_options(synchronize_session=False)
    )


async def ordenes_pendientes(
    session: AsyncSession, *, limite: int = 100
) -> list[ImportOutbox]:
    """Las órdenes que todavía no se entregaron, más viejas primero."""
    return list(
        (
            await session.execute(
                select(ImportOutbox)
                .where(ImportOutbox.published_at.is_(None))
                .order_by(ImportOutbox.created_at)
                .limit(limite)
            )
        )
        .scalars()
        .all()
    )


async def obtener_intento(
    session: AsyncSession, tenant_id: uuid.UUID, attempt_id: uuid.UUID
) -> ImportAttempt | None:
    """El intento, verificando el tenant. Nunca por id solo."""
    return (
        await session.execute(
            select(ImportAttempt).where(
                ImportAttempt.id == attempt_id,
                ImportAttempt.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()


__all__ = [
    "COMPLETADO",
    "LEASE_TTL_SEGUNDOS",
    "PAYLOAD_VERSION",
    "ResultadoDeRegistro",
    "SolicitudEnConflictoError",
    "cerrar_intento",
    "liberar_huerfanos",
    "marcar_publicada",
    "marcar_publicada_del_intento",
    "obtener_intento",
    "ordenes_pendientes",
    "reclamar_intento",
    "reencolar_orden",
    "registrar_intento",
    "renovar_lease",
]
