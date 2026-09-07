"""
Celery workers: file ingestion pipeline.

Three tasks, one per file category:
  - process_spreadsheet  : xlsx / csv
  - process_text_document: txt / docx
  - process_image_ocr    : jpg / png / heic

All tasks follow the same contract:
  1. Load UploadedFile record from DB (fail if not found).
  2. Set processing_status = PROCESSING.
  3. Download content from S3.
  4. Parse / extract data.
  5. Save parsed_summary_json + final processing_status to DB.
  6. Commit.

Confidence levels:  HIGH | MEDIUM | LOW
processing_status after parse: NEEDS_CONFIRMATION (always — human reviews before import).
On unrecoverable error: processing_status = FAILED.
"""

import asyncio
import time
import uuid as _uuid
from typing import Any

from app.application.services import pipeline_event_service
from app.application.services.file_parsing import (
    analyze_headers,
    extract_amounts_from_text,
    parse_uploaded_content,
)
from app.application.services.llm_file_type_detector import maybe_detect_file_type
from app.application.services.validation_gate import ValidationGate
from app.jobs.celery_app import celery_app
from app.observability.logger import bind_request_context, get_logger, log_job
from app.observability.metrics import track_job_event
from app.persistence.models.file import (
    PROCESSING_STATUS_FAILED,
    PROCESSING_STATUS_NEEDS_CONFIRMATION,
    PROCESSING_STATUS_PENDING,
    PROCESSING_STATUS_PROCESSING,
    PROCESSING_STATUS_REJECTED,
)
from app.persistence.models.pipeline_event import STAGE_VALIDATE

logger = get_logger(__name__)


def _analyze_headers(headers: list[str]) -> dict[str, Any]:
    """Backward-compatible wrapper for legacy imports in tests and workers."""
    return analyze_headers(headers)


def _extract_amounts_from_text(text: str) -> dict[str, Any]:
    """Backward-compatible wrapper for legacy imports in tests and workers."""
    return extract_amounts_from_text(text)


# ── Shared async helpers ──────────────────────────────────────────────────────


class ParseOwnershipLostError(RuntimeError):
    """El worker perdió la propiedad del parseo entre que lo tomó y quiso escribir.

    No es un error del archivo ni del parser: significa que otro intento reclamó
    el trabajo mientras éste seguía corriendo. Lo correcto es no escribir nada —
    el dueño actual es quien tiene que decidir el estado final.
    """


async def _claim_for_processing(
    session: Any, file_id: str, tenant_id: str
) -> tuple[Any, _uuid.UUID] | None:
    """Adquisición ATÓMICA del parseo. ``None`` = no era nuestro para tomar.

    Antes esto era un ``SELECT`` plano seguido de ``record.processing_status =
    PROCESSING``, incondicional: cualquier estado —``DONE`` incluido— se pisaba.
    Con ``task_acks_late=True`` una re-entrega alcanzaba para devolver a
    ``PROCESSING`` un archivo ya confirmado y dejarlo en ``NEEDS_CONFIRMATION``,
    que es un estado que el CAS de ``acquire_import_lease`` acepta.

    Sólo ``PENDING`` es reclamable, y es suficiente: los dos encolados pasan por
    ahí (`ingestion.py`, upload y ``reprocess_file``, que devuelve a ``PENDING``
    antes de reencolar). Un archivo ya ``PROCESSING`` NO se roba acá: eso lo
    decide el camino de staleness del endpoint, que exige que ``updated_at``
    tenga más de 300 s. Un archivo borrado tampoco — mismo criterio que
    ``acquire_import_lease``.

    Devuelve el registro y el token de propiedad que hay que presentar en cada
    escritura de resultado.
    """
    from sqlalchemy import select, update  # noqa: PLC0415

    from app.persistence.models.file import UploadedFile  # noqa: PLC0415

    token = _uuid.uuid4()
    claim = await session.execute(
        update(UploadedFile)
        .where(
            UploadedFile.id == _uuid.UUID(file_id),
            UploadedFile.tenant_id == _uuid.UUID(tenant_id),
            UploadedFile.deleted_at.is_(None),
            UploadedFile.processing_status == PROCESSING_STATUS_PENDING,
        )
        .values(
            processing_status=PROCESSING_STATUS_PROCESSING,
            parse_attempt_id=token,
        )
    )
    if claim.rowcount != 1:
        # Diagnóstico: distinguir "no existe" de "otro se lo llevó" o "borrado".
        # Es una consulta de más SÓLO en el camino que ya no va a trabajar.
        actual = (
            await session.execute(
                select(UploadedFile.processing_status, UploadedFile.deleted_at).where(
                    UploadedFile.id == _uuid.UUID(file_id),
                    UploadedFile.tenant_id == _uuid.UUID(tenant_id),
                )
            )
        ).first()
        logger.info(
            "ingestion.claim.skipped",
            file_id=file_id,
            tenant_id=tenant_id,
            reason=(
                "not_found"
                if actual is None
                else ("deleted" if actual.deleted_at is not None else "not_pending")
            ),
            current_status=None if actual is None else actual.processing_status,
        )
        return None

    record = (
        await session.execute(
            select(UploadedFile).where(
                UploadedFile.id == _uuid.UUID(file_id),
                # Redundante con el UPDATE de arriba (el id es PK), pero la regla
                # es que toda consulta de negocio lleve el tenant: que se cumpla
                # sin excepciones es lo que la hace revisable de un vistazo.
                UploadedFile.tenant_id == _uuid.UUID(tenant_id),
            )
        )
    ).scalar_one()
    # FASE 0: bindear trace_id del record (los contextvars no cruzan el boundary de Celery).
    if record.trace_id is not None:
        bind_request_context(trace_id=record.trace_id)
    return record, token


async def _load_owned(session: Any, file_id: str, tenant_id: str, token: _uuid.UUID) -> Any:
    """Relee el registro exigiendo que siga siendo NUESTRO.

    Es una lectura, así que no cierra sola la ventana con la escritura: el
    fencing real lo hace el ``UPDATE`` condicional de ``_save_result``. Sirve
    para cortar temprano —y para tener el ``trace_id`` con el que emitir la
    traza— sin llegar a hacer trabajo que después no se va a poder guardar.
    """
    from sqlalchemy import select  # noqa: PLC0415

    from app.persistence.models.file import UploadedFile  # noqa: PLC0415

    record = (
        await session.execute(
            select(UploadedFile).where(
                UploadedFile.id == _uuid.UUID(file_id),
                UploadedFile.tenant_id == _uuid.UUID(tenant_id),
                UploadedFile.deleted_at.is_(None),
                UploadedFile.parse_attempt_id == token,
                UploadedFile.processing_status == PROCESSING_STATUS_PROCESSING,
            )
        )
    ).scalar_one_or_none()
    if record is None:
        raise ParseOwnershipLostError(
            f"UploadedFile {file_id}: el parseo ya no es de este intento "
            "(otro dueño, otro estado, o el archivo se eliminó)"
        )
    return record


async def _save_result(
    session: Any,
    record: Any,
    summary: dict[str, Any],
    processing_status: str,
    *,
    token: _uuid.UUID,
    emit_stage: str | None = None,
    rejection_reason: str | None = None,
) -> None:
    """Escribe el resultado del parseo SÓLO si este intento sigue siendo el dueño.

    Cubre los tres finales —éxito, rechazo y error— porque los tres son igual de
    corruptos si llegan tarde: un ``FAILED`` del worker viejo sobre un archivo que
    otro intento ya dejó en ``NEEDS_CONFIRMATION`` borra un resultado bueno igual
    que lo haría un ``DONE``.

    El ``WHERE`` lleva el token Y el estado: sin el token no se distinguen dos
    intentos que están ambos en ``PROCESSING`` (el caso que deja ``reprocess_file``
    al recuperar un archivo trabado), y sin el estado un token viejo podría
    escribir sobre un archivo que ya salió de ``PROCESSING`` por otra vía.
    """
    from sqlalchemy import update  # noqa: PLC0415
    from sqlalchemy.orm.attributes import set_committed_value  # noqa: PLC0415

    from app.persistence.models.file import UploadedFile  # noqa: PLC0415

    valores: dict[str, Any] = {
        "parsed_summary_json": summary,
        "processing_status": processing_status,
    }
    if rejection_reason is not None:
        valores["rejection_reason"] = rejection_reason
    written = await session.execute(
        update(UploadedFile)
        .where(
            UploadedFile.id == record.id,
            UploadedFile.tenant_id == record.tenant_id,
            # El borrado puede ocurrir DESPUÉS del claim: la condición no alcanza
            # con comprobarla al adquirir. Y va acá, en la escritura, no sólo en
            # `_load_owned` — la lectura no cierra la ventana, sólo corta temprano.
            UploadedFile.deleted_at.is_(None),
            UploadedFile.parse_attempt_id == token,
            UploadedFile.processing_status == PROCESSING_STATUS_PROCESSING,
        )
        .values(**valores)
    )
    if written.rowcount != 1:
        raise ParseOwnershipLostError(
            f"UploadedFile {record.id}: el resultado no se escribió "
            "(propiedad perdida, estado cambiado o archivo eliminado)"
        )
    # El UPDATE de Core no toca el objeto en sesión. Se sincroniza como YA
    # COMMITEADO (no como cambio sucio): marcarlo sucio haría que el flush
    # siguiente lo reescriba con un UPDATE sin el WHERE del token, que es
    # justamente el fencing que se acaba de aplicar. Y `expire()` tampoco sirve:
    # el próximo acceso a un atributo dispararía IO implícito, que en AsyncSession
    # revienta con MissingGreenlet — y acá abajo se leen `trace_id` y `tenant_id`.
    for _campo, _valor in valores.items():
        set_committed_value(record, _campo, _valor)
    if emit_stage is not None:
        await pipeline_event_service.emit_event(
            session,
            trace_id=record.trace_id or record.id,
            tenant_id=record.tenant_id,
            stage=emit_stage,
            file_id=record.id,
            rows_out=summary.get("row_count") or summary.get("rows_processed"),
            confidence=summary.get("confidence"),
            detail={"passed": True, "file_type": summary.get("file_type")},
        )


#: Códigos de S3/R2 que NO mejoran reintentando: la clave no está, no hay permiso,
#: el objeto está archivado. Reintentar tres veces sólo retrasa el FAILED.
_S3_PERMANENTES = frozenset(
    {
        "NoSuchKey",
        "NoSuchBucket",
        "AccessDenied",
        "InvalidObjectState",
        "InvalidAccessKeyId",
        "SignatureDoesNotMatch",
    }
)


def _es_transitorio(exc: BaseException) -> bool:
    """¿Este error puede desaparecer si se vuelve a intentar?

    **El default es NO.** Un error de parseo o de validación va a fallar igual las
    tres veces: reintentarlo no arregla el archivo, retrasa el diagnóstico y
    ocupa el worker. Sólo se reintenta lo que depende de algo externo que puede
    estar caído un rato — la red, S3/R2, la base.

    Es lo que faltaba para que `max_retries=3` significara algo: estaba declarado
    en las tres tasks pero sin `bind=True`, sin `self.retry()` y sin
    `autoretry_for`, así que ninguna reintentaba nunca. `task_acks_late=True` da
    re-entrega si el worker MUERE, que es otra cosa: con una excepción, el archivo
    iba directo a FAILED.
    """
    from botocore.exceptions import BotoCoreError, ClientError  # noqa: PLC0415
    from sqlalchemy.exc import DBAPIError  # noqa: PLC0415

    # BotoCoreError cubre timeouts, DNS y fallos de conexión (no respuestas HTTP).
    if isinstance(exc, BotoCoreError):
        return True
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {}) if isinstance(exc.response, dict) else {}
        if str(error.get("Code", "")) in _S3_PERMANENTES:
            return False
        meta = exc.response.get("ResponseMetadata", {}) if isinstance(exc.response, dict) else {}
        status = meta.get("HTTPStatusCode")
        # Sin status (no llegó respuesta) o 5xx/429: del lado del servicio.
        return status is None or int(status) >= 500 or int(status) == 429
    if isinstance(exc, DBAPIError):
        # `connection_invalidated` es el corte de conexión (Neon cierra las ociosas).
        # Un error de integridad o de sintaxis SQL no es transitorio y cae en False.
        return bool(getattr(exc, "connection_invalidated", False))
    return isinstance(exc, ConnectionError | TimeoutError)


def _espera_antes_de_reintentar(intentos_previos: int) -> int:
    """Espera creciente: 30 s, 60 s, 120 s. Un servicio caído no se recupera en
    tres segundos, y machacarlo cada 30 s empeora una caída por saturación."""
    return int(30 * (2**intentos_previos))


async def _release_for_retry(
    factory: Any, file_id: str, tenant_id: str, token: _uuid.UUID
) -> bool:
    """Devuelve el archivo a PENDING para que el reintento lo pueda reclamar.

    Sin esto el reintento no serviría de nada: `_claim_for_processing` sólo toma
    archivos en PENDING, así que el segundo intento encontraría el archivo en
    PROCESSING —el estado en el que lo dejó el intento que falló— y saldría sin
    hacer nada.

    Lleva el mismo fencing que el resto: sólo libera si este intento sigue siendo
    el dueño. Devuelve ``False`` si ya no lo es, y entonces NO hay que reintentar
    — otro intento se quedó con el trabajo y reintentar sería competirle.
    """
    from sqlalchemy import update  # noqa: PLC0415

    from app.persistence.models.file import UploadedFile  # noqa: PLC0415

    async with factory() as session:
        liberado = await session.execute(
            update(UploadedFile)
            .where(
                UploadedFile.id == _uuid.UUID(file_id),
                UploadedFile.tenant_id == _uuid.UUID(tenant_id),
                UploadedFile.deleted_at.is_(None),
                UploadedFile.parse_attempt_id == token,
                UploadedFile.processing_status == PROCESSING_STATUS_PROCESSING,
            )
            .values(processing_status=PROCESSING_STATUS_PENDING, parse_attempt_id=None)
        )
        await session.commit()
    return bool(liberado.rowcount == 1)


async def _record_failure(
    factory: Any,
    file_id: str,
    tenant_id: str,
    token: _uuid.UUID | None,
    summary: dict[str, Any],
    task_name: str,
    t0: float,
    error: str,
) -> None:
    """Marca ``FAILED``, siempre que este intento siga siendo el dueño.

    Dos salidas sin escribir, ambas correctas:

    * ``token is None`` — el fallo ocurrió ANTES de reclamar el trabajo (no se
      pudo abrir la sesión, el archivo no era reclamable). No hay propiedad, y
      escribir sin ella es justamente lo que se vino a cerrar.
    * propiedad perdida — otro intento se quedó con el archivo mientras éste
      fallaba. Su ``FAILED`` tardío borraría un resultado que puede estar bien.

    Nunca tapa la excepción original: el caller la re-lanza después.
    """
    if token is None:
        logger.warning(
            "ingestion.failure_not_recorded",
            task=task_name,
            file_id=file_id,
            tenant_id=tenant_id,
            reason="never_claimed",
        )
        return
    try:
        async with factory() as session:
            record = await _load_owned(session, file_id, tenant_id, token)
            await _save_result(
                session,
                record,
                summary,
                PROCESSING_STATUS_FAILED,
                token=token,
            )
            await track_job_event(
                session,
                task_name,
                _uuid.UUID(tenant_id),
                success=False,
                duration_ms=int((time.monotonic() - t0) * 1000),
                error=error,
            )
            await session.commit()
    except ParseOwnershipLostError:
        logger.warning(
            "ingestion.failure_not_recorded",
            task=task_name,
            file_id=file_id,
            tenant_id=tenant_id,
            reason="ownership_lost",
        )


def _build_async_session(database_url: str) -> Any:
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: PLC0415
    from sqlalchemy.orm import sessionmaker  # noqa: PLC0415

    from app.config.settings import get_settings  # noqa: PLC0415

    engine = create_async_engine(
        database_url,
        pool_pre_ping=True,
        connect_args=get_settings().pg_connect_args,
    )
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)  # type: ignore[call-overload]
    return engine, factory


async def _apply_validation_gate(
    factory: Any,
    file_id: str,
    tenant_id: str,
    summary: dict[str, Any],
    t0: float,
    task_name: str,
    force: bool = False,
    *,
    token: _uuid.UUID,
) -> dict[str, Any] | None:
    """
    Runs ValidationGate on the parsed summary.
    Returns the (possibly corrected) summary if it passed, or None if REJECTED.
    Persists REJECTED status + rejection_reason to DB if gate fails.

    El rechazo es una escritura de resultado como cualquier otra y pasa por el
    mismo fencing: un ``REJECTED`` tardío pisa un resultado bueno igual que un
    ``FAILED`` tardío.
    """
    gate = ValidationGate()
    result = gate.validate(summary, force=force)

    if not result.passed:
        async with factory() as session:
            record = await _load_owned(session, file_id, tenant_id, token)
            await _save_result(
                session,
                record,
                summary,
                PROCESSING_STATUS_REJECTED,
                token=token,
                rejection_reason=result.rejection_reason,
            )
            await pipeline_event_service.emit_event(
                session,
                trace_id=record.trace_id or record.id,
                tenant_id=record.tenant_id,
                stage=STAGE_VALIDATE,
                file_id=record.id,
                rows_rejected=summary.get("row_count"),
                detail={"passed": False, "reason": result.rejection_reason},
            )
            await track_job_event(
                session,
                task_name,
                _uuid.UUID(tenant_id),
                success=False,
                duration_ms=int((time.monotonic() - t0) * 1000),
                error=f"REJECTED:{result.rejection_reason}",
            )
            await session.commit()
        logger.info(
            "ingestion.validation_gate.rejected",
            file_id=file_id,
            tenant_id=tenant_id,
            reason=result.rejection_reason,
        )
        return None

    return result.corrected_summary if result.corrected_summary is not None else summary


# ── Celery tasks ──────────────────────────────────────────────────────────────


@celery_app.task(  # type: ignore[misc]
    name="jobs.process_spreadsheet",
    queue="ingestion",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    soft_time_limit=150,
    time_limit=180,
)
def process_spreadsheet(self: Any, file_id: str, tenant_id: str, force: bool = False) -> None:
    """Parse .xlsx or .csv file and extract ventas/gastos/productos."""
    from app.config.settings import get_settings  # noqa: PLC0415

    s = get_settings()

    async def _run() -> BaseException | None:
        from app.integrations.s3 import S3Client  # noqa: PLC0415

        engine, factory = _build_async_session(s.DATABASE_URL)
        t0 = time.monotonic()
        # Declarado acá porque el manejo de error lo consulta: si el fallo ocurrió
        # ANTES de reclamar el trabajo, no hay propiedad y no hay nada que marcar.
        token: _uuid.UUID | None = None
        try:
            with log_job("jobs.process_spreadsheet", tenant_id=tenant_id, logger=logger):
                async with factory() as session:
                    reclamado = await _claim_for_processing(session, file_id, tenant_id)
                    if reclamado is None:
                        # Ya lo tomó otro intento, o el archivo no está en
                        # condiciones de parsearse. No es un error: salir sin
                        # tocar nada es exactamente lo correcto.
                        await session.commit()
                        return None
                    record, token = reclamado
                    await session.commit()

                # Download from S3
                s3 = S3Client()
                content = await s3.download(record.s3_key)

                # Parse
                summary = parse_uploaded_content(
                    content,
                    record.content_type,
                    record.original_filename,
                )

                # Validation gate — REJECTED if confidence too low or schema invalid
                validated_summary = await _apply_validation_gate(
                    factory,
                    file_id,
                    tenant_id,
                    summary,
                    t0,
                    "jobs.process_spreadsheet",
                    force=force,
                    token=token,
                )
                if validated_summary is None:
                    return None  # REJECTED — already persisted

                async with factory() as session:
                    result_record = await _load_owned(session, file_id, tenant_id, token)
                    # FASE 2 (A1): si el tipo quedó ambiguo ("general"), el LLM lo
                    # desambigua por contenido (fail-silent, solo si el flag está on).
                    await maybe_detect_file_type(
                        session,
                        validated_summary,
                        trace_id=result_record.trace_id or result_record.id,
                        tenant_id=result_record.tenant_id,
                        file_id=result_record.id,
                    )
                    await _save_result(
                        session,
                        result_record,
                        validated_summary,
                        PROCESSING_STATUS_NEEDS_CONFIRMATION,
                        token=token,
                        emit_stage=STAGE_VALIDATE,
                    )
                    await track_job_event(
                        session,
                        "jobs.process_spreadsheet",
                        _uuid.UUID(tenant_id),
                        success=True,
                        duration_ms=int((time.monotonic() - t0) * 1000),
                    )
                    await session.commit()

                logger.info(
                    "ingestion.spreadsheet.done",
                    file_id=file_id,
                    tenant_id=tenant_id,
                    confidence=validated_summary.get("confidence"),
                    rows=validated_summary.get("rows_processed"),
                )

                return None

        except ParseOwnershipLostError as perdida:
            # Final legítimo, no un fallo: otro intento se quedó con el trabajo.
            # No se re-lanza — hacerlo marcaría la task como fallida y, con los
            # reintentos de la entrega 3, la haría volver a competir por un
            # archivo que ya tiene dueño.
            logger.warning(
                "ingestion.parse.ownership_lost",
                task="jobs.process_spreadsheet",
                file_id=file_id,
                tenant_id=tenant_id,
                detail=str(perdida),
            )
            return None
        except Exception as exc:
            logger.error(
                "ingestion.spreadsheet.failed",
                file_id=file_id,
                tenant_id=tenant_id,
                error=str(exc),
                transitorio=_es_transitorio(exc),
                intento=int(self.request.retries or 0),
            )
            if (
                _es_transitorio(exc)
                and int(self.request.retries or 0) < int(self.max_retries or 0)
                and token is not None
                and await _release_for_retry(factory, file_id, tenant_id, token)
            ):
                # Vuelve a PENDING y se reintenta: NO se marca FAILED. Marcarlo
                # sería mentir sobre un archivo que todavía tiene intentos, y
                # además lo dejaría en un estado que el claim no puede tomar.
                return exc
            await _record_failure(
                factory,
                file_id,
                tenant_id,
                token,
                {"error": str(exc), "file_type": "spreadsheet"},
                "jobs.process_spreadsheet",
                t0,
                str(exc),
            )
            raise
        finally:
            await engine.dispose()

    # `self.retry()` se levanta ACÁ y no adentro de `_run`: la excepción `Retry`
    # de Celery tiene que salir del contexto sync de la task, no atravesar el
    # `asyncio.run` de un corutina. `_run` sólo DECIDE (devolviendo la excepción
    # que justifica el reintento) y esta capa ejecuta.
    a_reintentar = asyncio.run(_run())
    if a_reintentar is not None:
        raise self.retry(
            exc=a_reintentar,
            countdown=_espera_antes_de_reintentar(int(self.request.retries or 0)),
        )


@celery_app.task(  # type: ignore[misc]
    name="jobs.process_text_document",
    queue="ingestion",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    soft_time_limit=150,
    time_limit=180,
)
def process_text_document(self: Any, file_id: str, tenant_id: str, force: bool = False) -> None:
    """Parse .txt, .docx, .pdf or .pptx file, extracting reusable context."""
    from app.config.settings import get_settings  # noqa: PLC0415

    s = get_settings()

    async def _run() -> BaseException | None:
        from app.integrations.s3 import S3Client  # noqa: PLC0415

        engine, factory = _build_async_session(s.DATABASE_URL)
        t0 = time.monotonic()
        # Declarado acá porque el manejo de error lo consulta: si el fallo ocurrió
        # ANTES de reclamar el trabajo, no hay propiedad y no hay nada que marcar.
        token: _uuid.UUID | None = None
        try:
            with log_job("jobs.process_text_document", tenant_id=tenant_id, logger=logger):
                async with factory() as session:
                    reclamado = await _claim_for_processing(session, file_id, tenant_id)
                    if reclamado is None:
                        # Ya lo tomó otro intento, o el archivo no está en
                        # condiciones de parsearse. No es un error: salir sin
                        # tocar nada es exactamente lo correcto.
                        await session.commit()
                        return None
                    record, token = reclamado
                    await session.commit()

                s3 = S3Client()
                content = await s3.download(record.s3_key)

                summary = parse_uploaded_content(
                    content,
                    record.content_type,
                    record.original_filename,
                )

                # Validation gate
                validated_summary = await _apply_validation_gate(
                    factory,
                    file_id,
                    tenant_id,
                    summary,
                    t0,
                    "jobs.process_text_document",
                    force=force,
                    token=token,
                )
                if validated_summary is None:
                    return None

                async with factory() as session:
                    result_record = await _load_owned(session, file_id, tenant_id, token)
                    await _save_result(
                        session,
                        result_record,
                        validated_summary,
                        PROCESSING_STATUS_NEEDS_CONFIRMATION,
                        token=token,
                        emit_stage=STAGE_VALIDATE,
                    )
                    await track_job_event(
                        session,
                        "jobs.process_text_document",
                        _uuid.UUID(tenant_id),
                        success=True,
                        duration_ms=int((time.monotonic() - t0) * 1000),
                    )
                    await session.commit()

                logger.info(
                    "ingestion.text_document.done",
                    file_id=file_id,
                    tenant_id=tenant_id,
                    source_format=validated_summary.get("source_format"),
                    row_count=validated_summary.get("row_count"),
                )

                return None

        except ParseOwnershipLostError as perdida:
            # Final legítimo, no un fallo: otro intento se quedó con el trabajo.
            # No se re-lanza — hacerlo marcaría la task como fallida y, con los
            # reintentos de la entrega 3, la haría volver a competir por un
            # archivo que ya tiene dueño.
            logger.warning(
                "ingestion.parse.ownership_lost",
                task="jobs.process_text_document",
                file_id=file_id,
                tenant_id=tenant_id,
                detail=str(perdida),
            )
            return None
        except Exception as exc:
            logger.error(
                "ingestion.text_document.failed",
                file_id=file_id,
                tenant_id=tenant_id,
                error=str(exc),
                transitorio=_es_transitorio(exc),
                intento=int(self.request.retries or 0),
            )
            if (
                _es_transitorio(exc)
                and int(self.request.retries or 0) < int(self.max_retries or 0)
                and token is not None
                and await _release_for_retry(factory, file_id, tenant_id, token)
            ):
                # Vuelve a PENDING y se reintenta: NO se marca FAILED. Marcarlo
                # sería mentir sobre un archivo que todavía tiene intentos, y
                # además lo dejaría en un estado que el claim no puede tomar.
                return exc
            await _record_failure(
                factory,
                file_id,
                tenant_id,
                token,
                {"error": str(exc), "file_type": "text"},
                "jobs.process_text_document",
                t0,
                str(exc),
            )
            raise
        finally:
            await engine.dispose()

    # `self.retry()` se levanta ACÁ y no adentro de `_run`: la excepción `Retry`
    # de Celery tiene que salir del contexto sync de la task, no atravesar el
    # `asyncio.run` de un corutina. `_run` sólo DECIDE (devolviendo la excepción
    # que justifica el reintento) y esta capa ejecuta.
    a_reintentar = asyncio.run(_run())
    if a_reintentar is not None:
        raise self.retry(
            exc=a_reintentar,
            countdown=_espera_antes_de_reintentar(int(self.request.retries or 0)),
        )


@celery_app.task(  # type: ignore[misc]
    name="jobs.process_image_ocr",
    queue="ingestion",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    soft_time_limit=150,
    time_limit=180,
)
def process_image_ocr(self: Any, file_id: str, tenant_id: str, force: bool = False) -> None:
    """
    Run OCR on an image file (.jpg, .png, .heic).

    Confidence is always LOW — never auto-imports without user confirmation.
    If pytesseract is not available, marks file NEEDS_CONFIRMATION with an
    explicit error message so the user can review manually.
    """
    from app.config.settings import get_settings  # noqa: PLC0415

    s = get_settings()

    async def _run() -> BaseException | None:
        from app.integrations.s3 import S3Client  # noqa: PLC0415

        engine, factory = _build_async_session(s.DATABASE_URL)
        t0 = time.monotonic()
        # Declarado acá porque el manejo de error lo consulta: si el fallo ocurrió
        # ANTES de reclamar el trabajo, no hay propiedad y no hay nada que marcar.
        token: _uuid.UUID | None = None
        try:
            with log_job("jobs.process_image_ocr", tenant_id=tenant_id, logger=logger):
                async with factory() as session:
                    reclamado = await _claim_for_processing(session, file_id, tenant_id)
                    if reclamado is None:
                        # Ya lo tomó otro intento, o el archivo no está en
                        # condiciones de parsearse. No es un error: salir sin
                        # tocar nada es exactamente lo correcto.
                        await session.commit()
                        return None
                    record, token = reclamado
                    await session.commit()

                s3 = S3Client()
                content = await s3.download(record.s3_key)

                summary = parse_uploaded_content(
                    content,
                    record.content_type,
                    record.original_filename,
                )

                # Validation gate — images are always LOW confidence, REJECTED unless force=True
                validated_summary = await _apply_validation_gate(
                    factory,
                    file_id,
                    tenant_id,
                    summary,
                    t0,
                    "jobs.process_image_ocr",
                    force=force,
                    token=token,
                )
                if validated_summary is None:
                    return None

                # ALWAYS NEEDS_CONFIRMATION for images — never auto-import
                async with factory() as session:
                    result_record = await _load_owned(session, file_id, tenant_id, token)
                    await _save_result(
                        session,
                        result_record,
                        validated_summary,
                        PROCESSING_STATUS_NEEDS_CONFIRMATION,
                        token=token,
                        emit_stage=STAGE_VALIDATE,
                    )
                    await track_job_event(
                        session,
                        "jobs.process_image_ocr",
                        _uuid.UUID(tenant_id),
                        success=True,
                        duration_ms=int((time.monotonic() - t0) * 1000),
                    )
                    await session.commit()

                logger.info(
                    "ingestion.image_ocr.done",
                    file_id=file_id,
                    tenant_id=tenant_id,
                    has_ocr="error" not in validated_summary,
                )

                return None

        except ParseOwnershipLostError as perdida:
            # Final legítimo, no un fallo: otro intento se quedó con el trabajo.
            # No se re-lanza — hacerlo marcaría la task como fallida y, con los
            # reintentos de la entrega 3, la haría volver a competir por un
            # archivo que ya tiene dueño.
            logger.warning(
                "ingestion.parse.ownership_lost",
                task="jobs.process_image_ocr",
                file_id=file_id,
                tenant_id=tenant_id,
                detail=str(perdida),
            )
            return None
        except Exception as exc:
            logger.error(
                "ingestion.image_ocr.failed",
                file_id=file_id,
                tenant_id=tenant_id,
                error=str(exc),
                transitorio=_es_transitorio(exc),
                intento=int(self.request.retries or 0),
            )
            if (
                _es_transitorio(exc)
                and int(self.request.retries or 0) < int(self.max_retries or 0)
                and token is not None
                and await _release_for_retry(factory, file_id, tenant_id, token)
            ):
                # Vuelve a PENDING y se reintenta: NO se marca FAILED. Marcarlo
                # sería mentir sobre un archivo que todavía tiene intentos, y
                # además lo dejaría en un estado que el claim no puede tomar.
                return exc
            await _record_failure(
                factory,
                file_id,
                tenant_id,
                token,
                {"error": str(exc), "file_type": "image", "confidence": "LOW"},
                "jobs.process_image_ocr",
                t0,
                str(exc),
            )
            raise
        finally:
            await engine.dispose()

    # `self.retry()` se levanta ACÁ y no adentro de `_run`: la excepción `Retry`
    # de Celery tiene que salir del contexto sync de la task, no atravesar el
    # `asyncio.run` de un corutina. `_run` sólo DECIDE (devolviendo la excepción
    # que justifica el reintento) y esta capa ejecuta.
    a_reintentar = asyncio.run(_run())
    if a_reintentar is not None:
        raise self.retry(
            exc=a_reintentar,
            countdown=_espera_antes_de_reintentar(int(self.request.retries or 0)),
        )
