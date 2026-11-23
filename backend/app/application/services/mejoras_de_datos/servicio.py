"""Servicio de mejoras de datos: analizar / aplicar / revertir.

Sigue el patrón de ``reparacion_retiros_service``: ``analizar`` sólo lee;
``aplicar`` y ``revertir`` toman el lock de escritura del tenant, revalidan bajo
``FOR UPDATE`` (nunca confían en una vista previa vieja) y NO commitean: lo hace
el llamador (``get_db_session`` en un endpoint, la tarea Celery en el worker).

**Locks.** ``completa_vacios`` toma sólo el lock COMPARTIDO (convive con el POS y
con cualquier escritura común). ``cambia_valores`` toma el EXCLUSIVO, que frena
todas las escrituras del tenant hasta el commit: por eso va con ``lock_timeout``
y tandas cortas.

**Tandas.** ``aplicar`` procesa a lo sumo ``limite`` candidatos y devuelve cuántos
quedan; el llamador repite (commit entre una y otra) hasta que no queden. Cada
tanda es una «aplicación»: un ``DataRepairRun`` y una fila de auditoría por
entidad cambiada.

**Auditoría.** ``DataRepairItem.action`` es un CHECK cerrado (no hay «actualizar
costo»), así que el cambio por entidad se audita en ``decision_audit_log`` con
``decision_type = 'MEJORA_DE_DATOS:<id>'`` y ``before/after`` + ``run_id``. No se
toca ``ActionType`` (es el catálogo del agente, no de la auditoría).

**Lápida.** Una entidad con auditoría de la mejora no vuelve a entrar en la corrida
AUTOMÁTICA, aunque el detector la vuelva a ver (el caso: el usuario toca
«Deshacer» y el próximo ``POST /auto`` del Dashboard la reaplicaría). Sólo el
apply manual puede forzarla.

``tenant_id`` lo pasa el llamador (JWT en el endpoint, argumento en la tarea);
cada consulta filtra por él.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTasks

from app.application.services import maintenance_lock_service as mls
from app.application.services.mejoras_de_datos import registro
from app.application.services.mejoras_de_datos.contrato import (
    Candidato,
    Clase,
    Mejora,
    MotivoANoCargar,
    Reversion,
)
from app.observability.logger import get_logger
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.repair import DataRepairRun

logger = get_logger(__name__)

REPAIR_PREFIX: Final = "MEJORA:"
DECISION_APLICADA: Final = "MEJORA_DE_DATOS"
DECISION_REVERTIDA: Final = "MEJORA_DE_DATOS_REVERTIDA"
TRIGGERED_BY_SISTEMA: Final = "system:data_improvement"
TANDA: Final = 200
#: Cuánto espera el lock exclusivo antes de rendirse (sólo Postgres).
LOCK_TIMEOUT: Final = "3s"
#: Cuánto tiempo lo aplicado sigue visible (y deshacible) en el aviso.
VENTANA_APLICADAS_DIAS: Final = 14
MUESTRA: Final = 5


class MejoraDesconocidaError(LookupError):
    """El id no está en el registro."""


class CorridaInvalidaError(LookupError):
    """La corrida no existe, no es de una mejora o es de otro tenant."""


class ClaseIncorrectaError(ValueError):
    """Se intentó aplicar sola una mejora que cambia valores."""


@dataclass
class Pendiente:
    mejora_id: str
    titulo: str
    clase: Clase
    #: Todo lo que la mejora puede corregir hoy.
    cantidad: int
    #: De esos, los que el usuario ya deshizo: la corrida automática los saltea.
    en_lapida: int
    muestra: list[Candidato]
    a_mano: dict[str, str] = field(default_factory=dict)


@dataclass
class Aplicada:
    run_id: uuid.UUID
    mejora_id: str
    titulo: str
    status: str
    aplicadas: int
    creada: datetime
    resumen: dict[str, Any]

    @property
    def revertible(self) -> bool:
        return self.status == "APPLIED"


@dataclass
class Panel:
    pendientes: list[Pendiente]
    aplicadas: list[Aplicada]


@dataclass
class Resultado:
    run_id: uuid.UUID | None
    aplicadas: int
    #: Candidatos que quedaron para la próxima tanda.
    restantes: int
    a_mano: dict[str, str] = field(default_factory=dict)


def crear_s3() -> Any:
    """El cliente de S3 de las mejoras que leen el archivo original. Perezoso (no
    se construye al importar) y reemplazable en los tests."""
    from app.integrations.s3 import S3Client  # noqa: PLC0415

    return S3Client()


def _mejora(mejora_id: str) -> Mejora:
    m = registro.obtener(mejora_id)
    if m is None:
        raise MejoraDesconocidaError(mejora_id)
    return m


async def reunir_evidencia(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    mejora_id: str,
    ids: set[uuid.UUID] | None = None,
    s3: Any = None,
) -> dict[uuid.UUID, Any] | None:
    """La evidencia de archivo de una mejora que la necesita (``None`` si no).

    Se llama SIN lock: lee S3, y con el lock exclusivo tomado eso frenaría las
    escrituras del tenant durante una descarga. Una vez por request (el endpoint
    la reutiliza en todas las tandas)."""
    mejora = _mejora(mejora_id)
    if not mejora.necesita_archivo:
        return None
    det = await mejora.detectar(session, tenant_id, ids)
    if not det.candidatos:
        return {}
    return await mejora.evidencia(
        session, tenant_id, det.candidatos, s3 if s3 is not None else crear_s3()
    )


async def vista_previa(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    mejora_id: str,
    s3: Any = None,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """SELECT + lectura de S3, sin escribir: ``(filas a aplicar, «a mano» con motivo)``."""
    mejora = _mejora(mejora_id)
    det = await mejora.detectar(session, tenant_id)
    evidencia = (
        await mejora.evidencia(
            session, tenant_id, det.candidatos, s3 if s3 is not None else crear_s3()
        )
        if mejora.necesita_archivo and det.candidatos
        else {}
    )
    a_mano = dict(det.a_mano)
    con_evidencia = _separar_por_evidencia(mejora, det.candidatos, evidencia, a_mano)
    return await mejora.vista_previa(session, tenant_id, con_evidencia, evidencia), a_mano


def _separar_por_evidencia(
    mejora: Mejora,
    candidatos: list[Candidato],
    evidencia: dict[uuid.UUID, Any] | None,
    a_mano: dict[str, str],
) -> list[Candidato]:
    """Una mejora que necesita archivo sólo aplica lo que tiene evidencia; el resto
    queda «a mano» con su motivo (y NO cuenta como pendiente de la próxima tanda)."""
    if not mejora.necesita_archivo:
        return candidatos
    ok: list[Candidato] = []
    for c in candidatos:
        ev = (evidencia or {}).get(c.id)
        if ev is None or isinstance(ev, MotivoANoCargar):
            motivo = ev.motivo if isinstance(ev, MotivoANoCargar) else "sin evidencia del archivo"
            a_mano[f"{c.etiqueta} ({str(c.id)[:8]})"] = motivo
        else:
            ok.append(c)
    return ok


async def _tomar_lock(session: AsyncSession, tenant_id: uuid.UUID, mejora: Mejora) -> None:
    if mejora.clase == Clase.COMPLETA_VACIOS:
        await mls.acquire_write_lock_shared(session, tenant_id)
        return
    # El exclusivo bloquea hasta que no haya escrituras en vuelo: sin tope, un POS
    # lento deja esperando a la tarea (y ella frena al POS). Sólo existe en Postgres.
    if session.get_bind().dialect.name == "postgresql":
        await session.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
    await mls.acquire_maintenance_lock_exclusive(session, tenant_id)


async def ids_con_lapida(
    session: AsyncSession, tenant_id: uuid.UUID, mejora_id: str
) -> set[uuid.UUID]:
    """Entidades que esta mejora ya tocó alguna vez (aplicadas, aunque se deshicieran)."""
    filas = await session.execute(
        select(DecisionAuditLog.decision_data).where(
            DecisionAuditLog.tenant_id == tenant_id,
            DecisionAuditLog.decision_type == f"{DECISION_APLICADA}:{mejora_id}",
        )
    )
    out: set[uuid.UUID] = set()
    for (data,) in filas.all():
        try:
            out.add(uuid.UUID(str(data["entidad_id"])))
        except (KeyError, ValueError, TypeError):
            continue
    return out


async def analizar(session: AsyncSession, tenant_id: uuid.UUID) -> Panel:
    """SELECT-only: lo pendiente por mejora y lo aplicado en los últimos 14 días."""
    pendientes: list[Pendiente] = []
    titulos: dict[str, str] = {}
    for m in registro.todas():
        titulos[m.id] = m.titulo
        det = await m.detectar(session, tenant_id)
        if not det.candidatos and not det.a_mano:
            continue
        lapida = await ids_con_lapida(session, tenant_id, m.id)
        pendientes.append(
            Pendiente(
                mejora_id=m.id,
                titulo=m.titulo,
                clase=m.clase,
                cantidad=len(det.candidatos),
                en_lapida=sum(1 for c in det.candidatos if c.id in lapida),
                muestra=det.candidatos[:MUESTRA],
                a_mano=det.a_mano,
            )
        )
    desde = datetime.now(UTC) - timedelta(days=VENTANA_APLICADAS_DIAS)
    corridas = (
        await session.execute(
            select(DataRepairRun)
            .where(
                DataRepairRun.tenant_id == tenant_id,
                DataRepairRun.repair_type.like(f"{REPAIR_PREFIX}%"),
                DataRepairRun.dry_run.is_(False),
                DataRepairRun.created_at >= desde,
            )
            .order_by(DataRepairRun.created_at.desc())
        )
    ).scalars()
    aplicadas = [
        Aplicada(
            run_id=r.id,
            mejora_id=r.repair_type.removeprefix(REPAIR_PREFIX),
            titulo=titulos.get(r.repair_type.removeprefix(REPAIR_PREFIX), r.repair_type),
            status=r.status,
            aplicadas=int((r.details_json or {}).get("aplicadas", 0)),
            creada=r.created_at,
            resumen=dict(r.details_json or {}),
        )
        for r in corridas
    ]
    return Panel(pendientes=pendientes, aplicadas=aplicadas)


async def hay_candidatos_automaticos(session: AsyncSession, tenant_id: uuid.UUID) -> bool:
    """SELECT barato para ``POST /auto``: ¿hay algo que aplicar sin pedir permiso?"""
    for m in registro.todas():
        if m.clase != Clase.COMPLETA_VACIOS:
            continue
        det = await m.detectar(session, tenant_id)
        if not det.candidatos:
            continue
        lapida = await ids_con_lapida(session, tenant_id, m.id)
        if any(c.id not in lapida for c in det.candidatos):
            return True
    return False


async def registrar_cambio(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    run: DataRepairRun,
    mejora: Mejora,
    entidad_id: uuid.UUID,
    antes: dict[str, Any],
    despues: dict[str, Any],
    actor_user_id: uuid.UUID | None,
) -> None:
    """Una fila de auditoría por entidad cambiada. La usan las mejoras al aplicar."""
    session.add(
        DecisionAuditLog(
            tenant_id=tenant_id,
            decision_type=f"{DECISION_APLICADA}:{mejora.id}",
            decision_data={
                "run_id": str(run.id),
                "mejora_id": mejora.id,
                "entidad_id": str(entidad_id),
                "antes": antes,
                "despues": despues,
            },
            triggered_by=TRIGGERED_BY_SISTEMA if actor_user_id is None else "user",
            actor_user_id=actor_user_id,
            created_at=datetime.now(UTC),
        )
    )


async def aplicar(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    mejora_id: str,
    ids: set[uuid.UUID] | None = None,
    *,
    actor_user_id: uuid.UUID | None = None,
    automatica: bool = False,
    limite: int = TANDA,
    background: BackgroundTasks | None = None,
    evidencia: dict[uuid.UUID, Any] | None = None,
) -> Resultado:
    """Aplica UNA tanda. No commitea. ``ids`` acota a lo que eligió la vista previa.

    ``evidencia``: sólo para mejoras que leen un archivo. Si no viene se reúne ACÁ,
    antes del lock; el endpoint la reúne una vez y la pasa a todas las tandas."""
    mejora = _mejora(mejora_id)
    if automatica and mejora.clase != Clase.COMPLETA_VACIOS:
        raise ClaseIncorrectaError(mejora_id)
    if mejora.necesita_archivo and evidencia is None:
        evidencia = await reunir_evidencia(session, tenant_id, mejora_id, ids)
    await _tomar_lock(session, tenant_id, mejora)
    # Bajo el lock y ``FOR UPDATE``: se detecta DE NUEVO. Lo que vio la vista previa
    # pudo cambiar (otro usuario, otra tarea): aplica sólo la intersección.
    det = await mejora.detectar(session, tenant_id, ids, bloquear=True)
    a_mano = dict(det.a_mano)
    candidatos = _separar_por_evidencia(mejora, det.candidatos, evidencia, a_mano)
    if automatica:
        lapida = await ids_con_lapida(session, tenant_id, mejora.id)
        candidatos = [c for c in candidatos if c.id not in lapida]
    if not candidatos:
        return Resultado(run_id=None, aplicadas=0, restantes=0, a_mano=a_mano)
    candidatos = sorted(candidatos, key=lambda c: c.id)
    tanda = candidatos[: max(1, limite)]
    run = DataRepairRun(
        tenant_id=tenant_id,
        repair_type=f"{REPAIR_PREFIX}{mejora.id}",
        status="APPLIED",
        dry_run=False,
        candidates_found=len(tanda),
        completed_at=datetime.now(UTC),
        details_json={"automatica": automatica},
    )
    session.add(run)
    await session.flush()
    n = await mejora.aplicar(session, tenant_id, tanda, run, evidencia)
    run.details_json = {
        **(run.details_json or {}),
        "aplicadas": n,
        "a_mano": a_mano,
        "actor": str(actor_user_id) if actor_user_id else TRIGGERED_BY_SISTEMA,
    }
    await session.flush()
    if n:
        # Cambió costo/valuación: el score y el estado de negocio quedan viejos.
        from app.application.services.score_trigger_service import (  # noqa: PLC0415
            trigger_score_recalculation_after_commit,
        )

        trigger_score_recalculation_after_commit(
            session, str(tenant_id), "data_improvement", background
        )
    return Resultado(
        run_id=run.id,
        aplicadas=n,
        restantes=len(candidatos) - len(tanda),
        a_mano=a_mano,
    )


async def revertir(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    *,
    actor_user_id: uuid.UUID | None = None,
    background: BackgroundTasks | None = None,
) -> Reversion:
    """Deshace una corrida. No commitea. Idempotente: sólo reintenta los pendientes."""
    run = await session.get(DataRepairRun, run_id, with_for_update=True, populate_existing=True)
    if (
        run is None
        or run.tenant_id != tenant_id
        or not run.repair_type.startswith(REPAIR_PREFIX)
    ):
        raise CorridaInvalidaError(str(run_id))
    mejora = _mejora(run.repair_type.removeprefix(REPAIR_PREFIX))
    res = Reversion()
    if run.status == "REVERTED" or run.dry_run:
        return res
    await _tomar_lock(session, tenant_id, mejora)
    previo = await mejora.revertir(session, tenant_id, run)
    detalles = dict(run.details_json or {})
    ya = set(detalles.get("revertidos") or [])
    res = Reversion(revertidos=previo.revertidos, pendientes=previo.pendientes)
    detalles["revertidos"] = sorted(ya | set(res.revertidos))
    detalles["pendientes"] = res.pendientes
    run.details_json = detalles
    if res.completa:
        run.status = "REVERTED"
    session.add(
        DecisionAuditLog(
            tenant_id=tenant_id,
            decision_type=f"{DECISION_REVERTIDA}:{mejora.id}",
            decision_data={
                "run_id": str(run.id),
                "mejora_id": mejora.id,
                "revertidos": res.revertidos,
                "pendientes": res.pendientes,
                "completa": res.completa,
            },
            triggered_by=TRIGGERED_BY_SISTEMA if actor_user_id is None else "user",
            actor_user_id=actor_user_id,
            created_at=datetime.now(UTC),
        )
    )
    await session.flush()
    if res.revertidos:
        from app.application.services.score_trigger_service import (  # noqa: PLC0415
            trigger_score_recalculation_after_commit,
        )

        trigger_score_recalculation_after_commit(
            session, str(tenant_id), "data_improvement_revert", background
        )
    return res
