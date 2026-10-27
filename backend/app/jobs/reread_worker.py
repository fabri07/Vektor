"""Celery worker: apply de relectura (REREAD_FILE) en background.

El apply de un libro de compras grande inserta miles de filas (gastos + productos
+ movimientos + auditoría) y puede tardar minutos — demasiado para un request HTTP
(timeout → el usuario reintenta → duplicados). Esta task lo corre fuera del request:
el endpoint deja el ``DataRepairRun`` en QUEUED y encola; la task lo RECLAMA
(QUEUED → APPLYING) y ejecuta ``apply_reread`` reusando ese run, dejando status
APPLIED/FAILED. El frontend hace polling del estado.

Idempotente ante re-entrega (``task_acks_late``) vía un ``UPDATE`` atómico
condicionado por status, no una lectura+comparación en dos pasos — hallazgo de
code review: antes se leía ``run.status == "RUNNING"`` y LUEGO se escribía
``details_json["phase"]="applying"`` como dos operaciones separadas; dos
entregas del mismo mensaje (no solo un crash — también reentrega por red bajo
``task_acks_late``) podían leer "RUNNING" ANTES de que la otra commiteara su
propio cambio, y ambas seguían de largo aplicando la relectura dos veces. El
``UPDATE ... WHERE status='QUEUED'`` hace que solo UNA entrega gane la carrera
(``rowcount == 1``); cualquier otra encuentra el run ya en APPLYING/APPLIED/
FAILED y termina sin tocar datos.

Un crash a mitad dentro del ``try`` deja la transacción del apply sin
commitear (rollback) → el re-run (si Celery reintenta) arranca desde QUEUED de
nuevo — pero como este run YA quedó en APPLYING, no lo reclama otra entrega:
solo un reintento vía ``start_background_apply`` (usuario, o el sweep si
queda huérfano) genera un run NUEVO.
"""

from __future__ import annotations

import asyncio
import uuid as _uuid
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import update
from sqlalchemy.engine import CursorResult

from app.application.services.reread_limits import (
    REREAD_APPLY_HARD_LIMIT_SECONDS,
    REREAD_APPLY_SOFT_LIMIT_SECONDS,
)
from app.jobs.celery_app import celery_app
from app.jobs.ingestion_worker import _build_async_session
from app.observability.logger import bind_request_context, get_logger

logger = get_logger(__name__)


async def aplicar_relectura(run_id: str, file_id: str, tenant_id: str) -> dict[str, Any]:
    """El cuerpo de la task, aparte para poder esperarlo desde un test (la task
    lo corre con ``asyncio.run``, que no se puede anidar en un loop)."""
    from app.application.services import reread_service  # noqa: PLC0415
    from app.application.services.reread_service import (  # noqa: PLC0415
        _strip_bulky_fields,
    )
    from app.config.settings import get_settings  # noqa: PLC0415
    from app.persistence.models.repair import DataRepairRun  # noqa: PLC0415

    bind_request_context(tenant_id=tenant_id)
    engine, factory = _build_async_session(get_settings().DATABASE_URL)
    try:
        async with factory() as session:
            # Reclamo atómico: solo la entrega que gane el UPDATE (rowcount==1)
            # sigue de largo. Cualquier otra (reentrega, doble delivery) ve 0
            # filas afectadas y termina acá sin tocar datos de negocio.
            claim = await session.execute(
                update(DataRepairRun)
                .where(DataRepairRun.id == _uuid.UUID(run_id), DataRepairRun.status == "QUEUED")
                # Fase 10 (progreso con contexto): `updated_at` explícito —
                # este UPDATE es un statement Core (`sqlalchemy.update`), que
                # NO dispara el `onupdate` Python-side del modelo. Sin esto,
                # `applying_since` (servido al frontend) reflejaría el último
                # cambio de la sesión de PREVIEW, no el momento real en que
                # este run entró en APPLYING.
                .values(status="APPLYING", updated_at=datetime.now(UTC))
            )
            await session.commit()
            if cast("CursorResult[Any]", claim).rowcount == 0:
                run_check = await session.get(DataRepairRun, _uuid.UUID(run_id))
                status_found = run_check.status if run_check is not None else "MISSING"
                logger.info(
                    "reread.apply.skip_not_queued", run_id=run_id, status=status_found
                )
                return {"status": status_found}

            run = await session.get(DataRepairRun, _uuid.UUID(run_id))
            if run is None:
                logger.warning("reread.apply.run_missing", run_id=run_id)
                return {"status": "MISSING"}
            try:
                # F-RR: si el run viene de una sesión de preview, ya trae el
                # summary re-descargado/re-parseado cacheado — evita pagar
                # S3+parseo de nuevo acá (el worker corre en background, pero
                # sigue siendo trabajo evitable).
                fresh_override = (run.details_json or {}).get("fresh_summary")
                # Compuerta de relectura: la aceptación viaja en el run (no en
                # los argumentos de la task), así un worker viejo con una API
                # nueva —deploy en paralelo— no recibe un kwarg que no conoce.
                accept = bool((run.details_json or {}).get("accept_data_loss"))
                # El tope es lo que el usuario VIO al aceptar; sin una medición
                # previa, cero — aceptar algo que nunca se mostró no acepta nada.
                tope = (run.details_json or {}).get("data_loss") or {
                    "to_lose": 0,
                    "edited_at_risk": 0,
                }
                result = await reread_service.apply_reread(
                    session,
                    _uuid.UUID(file_id),
                    _uuid.UUID(tenant_id),
                    run=run,
                    fresh_override=fresh_override,
                    accept_data_loss=accept,
                    tope_aceptado=tope if accept else None,
                )
                await session.commit()
                logger.info(
                    "reread.apply.done",
                    run_id=run_id,
                    file_id=file_id,
                    voided=result.voided,
                    inserted=result.inserted,
                )
                return {"status": "APPLIED", "voided": result.voided}
            except reread_service.RereadWouldLoseDataError as exc:
                # No es una falla: se midió que se pierde algo y nadie lo
                # aceptó. Nada de lo aplicado queda; la sesión vuelve a estar
                # lista para aplicar, con la medición exacta para mostrar.
                await session.rollback()
                logger.info(
                    "reread.apply.data_loss_needs_confirmation",
                    run_id=run_id,
                    to_lose=exc.perdida.to_lose,
                    edited_at_risk=exc.perdida.edited_at_risk,
                )
                async with factory() as s2:
                    r = await s2.get(DataRepairRun, _uuid.UUID(run_id))
                    if r is not None and r.status == "APPLYING":
                        reread_service.volver_a_confirmar(r, exc.perdida)
                        await s2.commit()
                return {"status": "NEEDS_CONFIRMATION", "to_lose": exc.perdida.to_lose}
            except Exception as exc:  # noqa: BLE001
                await session.rollback()
                # B·5e-2: frenar porque dejaría ventas sin compra previa es una
                # regla de negocio, no una falla: info, no error (sin ruido en
                # Sentry). El run queda FAILED con el motivo para la pantalla.
                if isinstance(exc, reread_service.RereadWouldLeaveSalesWithoutPurchaseError):
                    logger.info("reread.apply.blocked_sales_without_purchase", run_id=run_id)
                elif isinstance(exc, reread_service.RereadFileDeletedError):
                    # I4-1: el archivo se borró con el run en la cola. Tampoco es
                    # una falla: no se aplica nada y el run queda cerrado.
                    logger.info("reread.apply.file_deleted", run_id=run_id)
                else:
                    logger.error(
                        "reread.apply.failed", run_id=run_id, error=str(exc)
                    )
                # Marcar FAILED en una transacción nueva (la anterior se revirtió).
                async with factory() as s2:
                    r = await s2.get(DataRepairRun, _uuid.UUID(run_id))
                    if r is not None and r.status == "APPLYING":
                        r.status = "FAILED"
                        r.completed_at = datetime.now(UTC)
                        details = _strip_bulky_fields(r.details_json or {})
                        details["error"] = str(exc)[:500]
                        if isinstance(exc, reread_service.RereadFileDeletedError):
                            details["reason"] = reread_service.MOTIVO_ARCHIVO_BORRADO
                        r.details_json = details
                        await s2.commit()
                return {"status": "FAILED", "error": str(exc)[:200]}
    finally:
        await engine.dispose()


# Los límites viven en `reread_limits` junto al umbral de "run abandonado" del
# servicio: los tres se mueven juntos o se abre una carrera. Ver ese módulo — los
# 300 s que había acá caían dentro del costo medido del apply de Asteria.
@celery_app.task(  # type: ignore[misc]
    name="jobs.reread_apply",
    queue="ingestion",
    max_retries=0,
    soft_time_limit=REREAD_APPLY_SOFT_LIMIT_SECONDS,
    time_limit=REREAD_APPLY_HARD_LIMIT_SECONDS,
)
def reread_apply(run_id: str, file_id: str, tenant_id: str) -> None:
    """Ejecuta ``reread_service.apply_reread`` sobre un run ya encolado."""

    asyncio.run(aplicar_relectura(run_id, file_id, tenant_id))
