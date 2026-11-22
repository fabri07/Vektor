"""Celery task: aplica, para UN tenant, las mejoras de datos que sólo completan vacíos.

La encola ``POST /data-improvements/auto`` (el Dashboard al cargar). No hay cron ni
hook de deploy a propósito: Beat no está desplegado y los deploys de api y worker
llegan en paralelo y sin orden. Si el worker todavía corre código viejo, la tarea
falla sin daño y el próximo disparo la reintenta.

Sin backend de resultados (``celery_app.backend_cls = 'disabled://'``): el estado
vive en la base (``DataRepairRun``), que es lo que lee el aviso.

Una mejora que falla NO corta a las otras (fail-soft). Cada tanda commitea aparte:
así el lock compartido se suelta entre tandas y el recosteo que dispara el commit
(listeners de ``costeo_de_ventas``) corre por tanda.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.tenant_status import ESTADOS_OPERATIVOS
from app.jobs.celery_app import celery_app
from app.observability.logger import get_logger, log_job

logger = get_logger(__name__)

#: Tope de tandas por mejora y por corrida: la tarea tiene ``time_limit``.
MAX_TANDAS = 25


async def procesar(
    session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> dict[str, int]:
    """Aplica las mejoras automáticas del tenant. Devuelve ``{mejora_id: aplicadas}``.

    Separada de la tarea para poder probarse con una fábrica de sesiones de test
    (la tarea arma su propio engine desde ``DATABASE_URL``).
    """
    from sqlalchemy import select  # noqa: PLC0415

    from app.application.services.mejoras_de_datos import registro, servicio  # noqa: PLC0415
    from app.application.services.mejoras_de_datos.contrato import Clase  # noqa: PLC0415
    from app.persistence.models.tenant import Tenant  # noqa: PLC0415

    async with session_factory() as session:
        estado = (
            await session.execute(select(Tenant.status).where(Tenant.tenant_id == tenant_id))
        ).scalar_one_or_none()
    if estado not in ESTADOS_OPERATIVOS:
        # Solo lectura o suspendido: nadie escribe en esa cuenta, tampoco el sistema.
        return {}

    totales: dict[str, int] = {}
    for mejora in registro.todas():
        if mejora.clase != Clase.COMPLETA_VACIOS:
            continue
        total = 0
        try:
            for _ in range(MAX_TANDAS):
                async with session_factory() as session:
                    r = await servicio.aplicar(
                        session, tenant_id, mejora.id, automatica=True
                    )
                    await session.commit()
                total += r.aplicadas
                if r.restantes <= 0:
                    break
        except Exception:  # noqa: BLE001 — una mejora rota no corta a las otras
            logger.exception(
                "mejoras_de_datos.fallo", tenant_id=str(tenant_id), mejora=mejora.id
            )
        totales[mejora.id] = total
    return totales


async def _run(tenant_id_str: str) -> None:
    from sqlalchemy.orm import sessionmaker  # noqa: PLC0415

    from app.config.settings import get_settings  # noqa: PLC0415
    from app.persistence.db.engine_factory import crear_engine  # noqa: PLC0415

    s = get_settings()
    engine = crear_engine(s.DATABASE_URL, pool_pre_ping=True, connect_args=s.pg_connect_args)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)  # type: ignore[call-overload]
    try:
        with log_job("jobs.aplicar_mejoras_de_datos", tenant_id=tenant_id_str, logger=logger):
            totales = await procesar(factory, uuid.UUID(tenant_id_str))
            logger.info(
                "mejoras_de_datos.aplicadas", tenant_id=tenant_id_str, totales=totales
            )
    finally:
        await engine.dispose()


@celery_app.task(  # type: ignore[misc]
    bind=True,
    name="jobs.aplicar_mejoras_de_datos",
    queue="scores",
    max_retries=2,
    default_retry_delay=120,
    soft_time_limit=240,
    time_limit=290,
)
def aplicar_mejoras_de_datos(self: Any, tenant_id: str) -> None:
    try:
        asyncio.run(_run(tenant_id))
    except Exception as exc:
        logger.warning(
            "mejoras_de_datos.retry",
            tenant_id=tenant_id,
            attempt=self.request.retries,
            error=str(exc),
        )
        raise self.retry(exc=exc) from exc
