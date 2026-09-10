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
from datetime import UTC, datetime, timedelta
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
    FALLADO,
    PENDIENTE,
    exigir_transicion,
    huella_de_solicitud,
)
from app.observability.logger import get_logger
from app.persistence.models.import_attempt import ImportAttempt, ImportOutbox

logger = get_logger(__name__)

#: Cuánto vale un lease de ejecución. Más largo que el import más lento medido
#: (5.000 filas ≈ 25 s) por dos órdenes de magnitud: el lease está para detectar
#: un ejecutor MUERTO, no para apurar a uno lento. Un TTL corto produce el peor
#: de los mundos — dos ejecutores corriendo lo mismo a la vez.
LEASE_TTL_SEGUNDOS = 15 * 60

_CONFLICTO_CLAVE = unique_violation_classifier(
    "request_key",
    constraint="uq_import_attempts_request_key",
    columns=("import_attempts.tenant_id", "import_attempts.request_key"),
)


@dataclass(frozen=True)
class ResultadoDeRegistro:
    """Qué pasó al registrar. ``creado=False`` significa "ya existía"."""

    intento: ImportAttempt
    creado: bool


class SolicitudEnConflictoError(Exception):
    """La misma clave de petición con un contenido distinto.

    No se puede resolver sola y por eso es un error y no una decisión: devolver el
    intento viejo importaría algo que el usuario no pidió, y crear uno nuevo
    rompería la promesa de la clave. Se le dice al cliente que use otra clave o
    que consulte el intento que ya existe.
    """

    def __init__(self, intento_existente: ImportAttempt) -> None:
        self.intento_existente = intento_existente
        super().__init__(
            "Ya hay una importación registrada con esta misma clave de petición "
            "pero con otro contenido."
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
        return _resolver_existente(existente, huella)

    intento = ImportAttempt(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        file_id=file_id,
        request_key=request_key,
        payload_hash=huella,
        payload_json=payload,
        ingestion_version=ingestion_version,
        preview_version=preview_version,
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
        return _resolver_existente(ganador, huella)

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


def _resolver_existente(intento: ImportAttempt, huella: str) -> ResultadoDeRegistro:
    if intento.payload_hash != huella:
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
            lease_expires_at=func.now() + timedelta(seconds=ttl_segundos),
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
    valores: dict[str, Any] = {
        "lease_expires_at": func.now() + timedelta(seconds=ttl_segundos)
    }
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


async def liberar_huerfanos(
    session: AsyncSession, *, limite: int = 50
) -> list[uuid.UUID]:
    """Devuelve a PENDIENTE los intentos cuyo ejecutor murió. Los ids liberados.

    Un ``EJECUTANDO`` con el lease vencido es, por definición, un ejecutor que no
    renovó: o murió, o se colgó tanto que ya no se le puede creer. Volverlo a
    ``PENDIENTE`` es lo que permite que el publicador lo entregue de nuevo.

    Los que ya agotaron ``max_attempts`` se marcan ``FALLADO`` en vez de volver a
    la cola: reintentar sin límite un trabajo que mata al ejecutor lo único que
    logra es matar a los siguientes.
    """
    vencidos = (
        (
            await session.execute(
                select(ImportAttempt)
                .where(
                    ImportAttempt.status == EJECUTANDO,
                    ImportAttempt.lease_expires_at < func.now(),
                )
                .limit(limite)
            )
        )
        .scalars()
        .all()
    )
    liberados: list[uuid.UUID] = []
    for intento in vencidos:
        agotado = intento.attempts >= intento.max_attempts
        intento.status = FALLADO if agotado else PENDIENTE
        intento.lease_token = None
        intento.lease_expires_at = None
        intento.phase = None
        if agotado:
            intento.error_code = "transitorio"
            intento.error_detail = (
                "La importación se interrumpió y ya se reintentó el máximo de "
                "veces. Volvé a confirmar el archivo."
            )
            intento.finished_at = datetime.now(UTC)
        else:
            liberados.append(intento.id)
    if vencidos:
        logger.info(
            "ingestion.intento.huerfanos",
            vencidos=len(vencidos),
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
    "ResultadoDeRegistro",
    "SolicitudEnConflictoError",
    "cerrar_intento",
    "liberar_huerfanos",
    "marcar_publicada",
    "obtener_intento",
    "ordenes_pendientes",
    "reclamar_intento",
    "registrar_intento",
    "renovar_lease",
]
