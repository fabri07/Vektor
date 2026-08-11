"""
Score trigger service — thin shim that dispatches Celery tasks.
Imported by API endpoints to schedule async recalculations.
"""

from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTasks

from app.jobs.celery_app import celery_app


def trigger_score_recalculation_after_commit(
    db: AsyncSession,
    tenant_id: str,
    triggered_by: str,
    background: BackgroundTasks | None = None,
) -> None:
    """Dispara el recálculo SOLO después de que la transacción commitee.

    El worker abre su PROPIA sesión y lee la base: si el task sale mientras la
    transacción del request sigue abierta, calcula contra un estado que todavía
    no existe —o que un rollback va a descartar— y persiste un score que nunca
    correspondió. Con un borrado de archivo el error es visible: el score se
    recalcularía con los datos que se están revirtiendo.

    Mismo mecanismo que ``EventBus.emit_after_commit`` (``stock_service``,
    ``pending_action_service``): el listener ``after_commit`` de SQLAlchemy. Si la
    transacción hace rollback, el task nunca se encola.

    ``background`` (las ``BackgroundTasks`` del endpoint) saca además el
    ``.delay()`` del camino de la respuesta. Sin él, la llamada al broker corre
    dentro del request —en el commit de ``get_db_session``— y un broker lento o
    caído lo paga el usuario esperando, en una operación que ya terminó. Con él,
    el commit solo AGENDA el encolado y Starlette lo ejecuta después de haber
    enviado la respuesta. El orden lo garantiza FastAPI ≥0.106: las dependencias
    con ``yield`` (y por lo tanto el commit) cierran ANTES de que se envíe la
    respuesta, y las background tasks corren después.

    Fuera de un request (worker de Celery, scripts) se llama sin ``background``:
    ahí no hay respuesta que proteger y el encolado va en línea con el commit.
    """
    sync_session = db.sync_session

    def _encolar() -> None:
        # Fail-safe OBLIGATORIO: acá la transacción YA commiteó. Si el broker está
        # caído, dejar propagar la excepción le devolvería un 500 al usuario por
        # una operación que sí se completó — y encima lo invitaría a repetirla.
        # El score se recalcula igual en el próximo evento o por el job periódico.
        # Vale igual como background task: ahí la excepción ni siquiera llegaría
        # al usuario (la respuesta ya salió), solo ensuciaría el log como error.
        try:
            trigger_score_recalculation.delay(tenant_id, triggered_by)
        except Exception:  # noqa: BLE001 — el recálculo nunca rompe la respuesta
            from app.observability.logger import get_logger  # noqa: PLC0415

            get_logger(__name__).warning(
                "score.recalculation_enqueue_failed",
                tenant_id=tenant_id,
                triggered_by=triggered_by,
            )

    @event.listens_for(sync_session, "after_commit", once=True)
    def _disparar(_session: Any) -> None:
        if background is not None:
            # `_encolar` es sync: Starlette lo corre en un threadpool, así que el
            # I/O bloqueante del broker no traba el event loop.
            background.add_task(_encolar)
            return
        _encolar()


@celery_app.task(name="jobs.trigger_score_recalculation", queue="scores")  # type: ignore[misc]
def trigger_score_recalculation(tenant_id: str, triggered_by: str) -> None:
    """
    Celery task entrypoint.
    Delegates to HealthScoreService inside a sync DB session.
    """
    import asyncio  # noqa: PLC0415
    import uuid  # noqa: PLC0415

    from app.config.settings import get_settings  # noqa: PLC0415
    from app.observability.logger import get_logger  # noqa: PLC0415

    log = get_logger(__name__)
    s = get_settings()

    # Use sync engine for Celery (psycopg2)
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: PLC0415
    from sqlalchemy.orm import sessionmaker  # noqa: PLC0415

    async def _run() -> None:
        engine = create_async_engine(
            s.DATABASE_URL, pool_pre_ping=True, connect_args=s.pg_connect_args
        )
        async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)  # type: ignore[call-overload]
        async with async_session() as session:
            from app.application.services.health_score_service import (
                HealthScoreService,  # noqa: PLC0415
            )

            svc = HealthScoreService(session)
            await svc.recalculate_for_tenant(
                tenant_id=uuid.UUID(tenant_id),
                triggered_by=triggered_by,
            )
            await session.commit()
        await engine.dispose()

    asyncio.run(_run())
    log.info("score_trigger.complete", tenant_id=tenant_id, triggered_by=triggered_by)
