"""Mejoras de datos: aplicar a lo ya cargado las correcciones que la ingesta ganó después.

Sin ``require_active_subscription`` a propósito: corregir datos que el cliente YA
cargó queda siempre abierto (mismo criterio que ``opening-confirmation``). Lo que
cambia un valor existente exige PIN (``require_modify_access``); lo que sólo
completa un vacío lo dispara el Dashboard sin PIN (``POST /auto``, actor sistema).

Contrato y reglas: ``app/application/services/mejoras_de_datos/servicio.py``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import (
    ensure_tenant_not_under_maintenance,
    get_current_tenant,
    get_current_user,
    require_modify_access,
    require_role,
)
from app.application.services.mejoras_de_datos import servicio
from app.observability.logger import get_logger
from app.persistence.db.redis_client import get_redis
from app.persistence.db.session import get_db_session
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User

router = APIRouter()
logger = get_logger(__name__)

#: Antidoble del disparo automático: a lo sumo un chequeo por tenant cada 10 min.
AUTO_TTL_SEGUNDOS = 600
AUTO_KEY = "mejoras_de_datos:auto:{tenant_id}"
#: Tandas máximas por request manual; si quedan, se informa ``restantes``.
MAX_TANDAS_MANUAL = 10


class CandidatoOut(BaseModel):
    id: uuid.UUID
    etiqueta: str
    detalle: dict[str, Any]


class PendienteOut(BaseModel):
    mejora_id: str
    titulo: str
    clase: str
    cantidad: int
    en_lapida: int
    muestra: list[CandidatoOut]
    a_mano: dict[str, str]


class AplicadaOut(BaseModel):
    run_id: uuid.UUID
    mejora_id: str
    titulo: str
    status: str
    aplicadas: int
    creada: datetime
    revertible: bool
    resumen: dict[str, Any]


class PanelOut(BaseModel):
    pendientes: list[PendienteOut]
    aplicadas: list[AplicadaOut]


class AplicarIn(BaseModel):
    #: ``None`` = todo lo que hoy detecta la mejora; si no, sólo lo elegido en la vista previa.
    ids: list[uuid.UUID] | None = Field(default=None, max_length=5000)


class AplicarOut(BaseModel):
    run_ids: list[uuid.UUID]
    aplicadas: int
    restantes: int
    a_mano: dict[str, str]


class RevertirOut(BaseModel):
    revertidos: list[str]
    pendientes: dict[str, str]
    completa: bool


class AutoOut(BaseModel):
    queued: bool


class VistaPreviaOut(BaseModel):
    #: Lo que se aplicaría hoy, con los números que verá el dueño.
    filas: list[dict[str, Any]]
    #: Lo que NO se puede cargar sin adivinar, con su motivo.
    a_mano: dict[str, str]


def _lock_ocupado(exc: OperationalError) -> bool:
    texto = str(exc.orig).lower()
    return "lock" in texto and ("timeout" in texto or "not available" in texto)


def _ocupado() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_423_LOCKED,
        detail="La cuenta está ocupada con otra operación. Probá de nuevo en un momento.",
    )


@router.get("", response_model=PanelOut, summary="Mejoras pendientes y lo aplicado (14 días)")
async def listar(
    tenant: Tenant = Depends(get_current_tenant),
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> PanelOut:
    """SELECT-only: no escribe nada, ni siquiera auditoría."""
    panel = await servicio.analizar(session, tenant.tenant_id)
    return PanelOut(
        pendientes=[
            PendienteOut(
                mejora_id=p.mejora_id,
                titulo=p.titulo,
                clase=str(p.clase),
                cantidad=p.cantidad,
                en_lapida=p.en_lapida,
                muestra=[
                    CandidatoOut(id=c.id, etiqueta=c.etiqueta, detalle=c.detalle)
                    for c in p.muestra
                ],
                a_mano=p.a_mano,
            )
            for p in panel.pendientes
        ],
        aplicadas=[
            AplicadaOut(
                run_id=a.run_id,
                mejora_id=a.mejora_id,
                titulo=a.titulo,
                status=a.status,
                aplicadas=a.aplicadas,
                creada=a.creada,
                revertible=a.revertible,
                resumen=a.resumen,
            )
            for a in panel.aplicadas
        ],
    )


@router.get(
    "/{mejora_id}/preview",
    response_model=VistaPreviaOut,
    summary="Vista previa de una mejora que lee el archivo original (no escribe)",
)
async def vista_previa(
    mejora_id: str,
    tenant: Tenant = Depends(get_current_tenant),
    _user: User = Depends(require_role("OWNER", "ADMIN")),
    session: AsyncSession = Depends(get_db_session),
) -> VistaPreviaOut:
    """SELECT + lectura de S3. Una descarga por archivo, con tope por request."""
    try:
        filas, a_mano = await servicio.vista_previa(session, tenant.tenant_id, mejora_id)
    except servicio.MejoraDesconocidaError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Mejora no encontrada.") from exc
    return VistaPreviaOut(filas=filas, a_mano=a_mano)


@router.post(
    "/auto",
    response_model=AutoOut,
    summary="Dispara las mejoras que sólo completan vacíos (lo llama el Dashboard)",
)
async def disparar_automaticas(
    background: BackgroundTasks,
    tenant: Tenant = Depends(get_current_tenant),
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
    redis: Redis = Depends(get_redis),
) -> AutoOut:
    """Encola UNA tarea por tenant si hay algo para completar. Nunca rompe el Dashboard.

    El ``SET NX`` va ANTES del SELECT: el Dashboard carga seguido y no hay por qué
    consultar en cada carga. Si Redis falla no se encola (sin antidoble, cada carga
    mandaría una tarea); es seguro porque la tarea revalida todo bajo lock.
    """
    tid = tenant.tenant_id
    try:
        tomado = await redis.set(
            AUTO_KEY.format(tenant_id=tid), "1", nx=True, ex=AUTO_TTL_SEGUNDOS
        )
    except Exception:  # noqa: BLE001 — el aviso es accesorio: jamás un 500 en el Dashboard
        logger.warning("mejoras_de_datos.auto_sin_redis", tenant_id=str(tid))
        return AutoOut(queued=False)
    if not tomado:
        return AutoOut(queued=False)
    if not await servicio.hay_candidatos_automaticos(session, tid):
        return AutoOut(queued=False)

    def _encolar() -> None:
        # La respuesta ya salió: un broker caído sólo ensucia el log. El próximo
        # disparo (TTL de 10 min) lo reintenta.
        try:
            from app.jobs.mejoras_de_datos import aplicar_mejoras_de_datos  # noqa: PLC0415

            aplicar_mejoras_de_datos.delay(str(tid))
        except Exception:  # noqa: BLE001
            logger.warning("mejoras_de_datos.auto_encolado_fallo", tenant_id=str(tid))

    background.add_task(_encolar)
    return AutoOut(queued=True)


@router.post(
    "/runs/{run_id}/revert",
    response_model=RevertirOut,
    summary="Deshacer una corrida de mejora",
)
async def revertir(
    run_id: uuid.UUID,
    background: BackgroundTasks,
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(require_modify_access),
    _maintenance_guard: None = Depends(ensure_tenant_not_under_maintenance),
    session: AsyncSession = Depends(get_db_session),
) -> RevertirOut:
    try:
        r = await servicio.revertir(
            session,
            tenant.tenant_id,
            run_id,
            actor_user_id=user.user_id,
            background=background,
        )
    except (servicio.CorridaInvalidaError, servicio.MejoraDesconocidaError) as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Corrida no encontrada.") from exc
    except OperationalError as exc:
        if _lock_ocupado(exc):
            raise _ocupado() from exc
        raise
    return RevertirOut(revertidos=r.revertidos, pendientes=r.pendientes, completa=r.completa)


@router.post(
    "/{mejora_id}/apply",
    response_model=AplicarOut,
    summary="Aplicar una mejora (a mano; las que cambian valores sólo se aplican así)",
)
async def aplicar(
    mejora_id: str,
    body: AplicarIn,
    background: BackgroundTasks,
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(require_modify_access),
    _maintenance_guard: None = Depends(ensure_tenant_not_under_maintenance),
    session: AsyncSession = Depends(get_db_session),
) -> AplicarOut:
    """Aplica por tandas (commit entre una y otra: el lock se suelta). Revalida bajo
    lock: si lo que vio la vista previa cambió, aplica sólo lo que sigue valiendo."""
    ids = set(body.ids) if body.ids is not None else None
    run_ids: list[uuid.UUID] = []
    aplicadas = 0
    restantes = 0
    a_mano: dict[str, str] = {}
    try:
        # Una sola lectura de S3 por request (fuera del lock), compartida por las tandas.
        evidencia = await servicio.reunir_evidencia(session, tenant.tenant_id, mejora_id, ids)
        for _ in range(MAX_TANDAS_MANUAL):
            r = await servicio.aplicar(
                session,
                tenant.tenant_id,
                mejora_id,
                ids,
                actor_user_id=user.user_id,
                background=background,
                evidencia=evidencia,
            )
            a_mano = r.a_mano
            restantes = r.restantes
            if r.run_id is not None:
                run_ids.append(r.run_id)
                aplicadas += r.aplicadas
            if r.restantes <= 0:
                break
            await session.commit()
    except servicio.MejoraDesconocidaError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Mejora no encontrada.") from exc
    except OperationalError as exc:
        if _lock_ocupado(exc):
            raise _ocupado() from exc
        raise
    return AplicarOut(run_ids=run_ids, aplicadas=aplicadas, restantes=restantes, a_mano=a_mano)

