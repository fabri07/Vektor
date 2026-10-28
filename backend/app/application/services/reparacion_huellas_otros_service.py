"""V-16: las huellas del lote de «Otros» que quedaron puestas sin importar nada.

Antes de #214, «Importar sugeridos» (``bulk_import_unclassified``) registraba la
huella ``unclassified:{id}`` ANTES de las compuertas: una fila que quedaba en
``needs_manual`` (fecha ilegible, sin stock, sin compra previa…) salía «ya
importada» (``skipped``) en cada corrida siguiente, para siempre. #214 arregló
el orden, pero las huellas viejas siguen ahí. Esto las limpia, con el OK del
dueño:

- **analizar** (sólo lectura): por negocio, las filas PENDING con la huella del
  lote registrada, separadas en *huérfanas* (ninguna venta ni gasto vivo nació de
  la fila) y *con respaldo* (hay uno vivo con ``source_row_ref =
  unclassified:{id}``). No toca nada.
- **reparar**: borra SÓLO las huérfanas, con los mismos candados que el lote
  (shared → import) y las filas tomadas ``FOR UPDATE``, y deja una
  ``DataRepairRun`` con cada huella borrada. Una huella con una operación viva
  nunca se borra: sin ella el lote podría volver a cargar la fila.
- **revertir(run_id)**: vuelve a poner las huellas de la corrida que no estén
  (si el lote ya importó la fila, su huella es la misma y no se duplica).

Borrar una huella no importa nada: la fila sigue PENDING y la próxima corrida del
lote la juzga con las compuertas de hoy (I2-4, I2-5, V-4).
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import maintenance_lock_service
from app.application.services.ingestion_import_service import ancla_de_fila_de_otros
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.memory import OperationFingerprint
from app.persistence.models.repair import DataRepairRun
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.persistence.models.unclassified_record import (
    UNCLASSIFIED_STATUS_PENDING,
    UnclassifiedRecord,
    unclassified_row_ref,
)

REPAIR_TYPE: Final = "V16_HUELLAS_OTROS"
#: Tamaño de tanda de los ``IN (...)``: lejos del límite de parámetros de asyncpg.
_TANDA: Final = 1000


@dataclass
class Huella:
    """Valores planos (no el ORM): el análisis se imprime después del rollback."""

    record_id: uuid.UUID
    fingerprint_id: uuid.UUID
    fingerprint: str
    action_type: str
    executed_at: datetime | None
    sugerida: str | None
    motivo: str | None


@dataclass
class Resumen:
    tenant_id: uuid.UUID
    huerfanas: list[Huella] = field(default_factory=list)
    #: Con una venta o un gasto vivo nacido de la fila: nunca se borran.
    con_respaldo: list[Huella] = field(default_factory=list)
    run_id: uuid.UUID | None = None


def _huella_de(tenant_id: uuid.UUID, record_id: uuid.UUID) -> str:
    return hashlib.sha256(ancla_de_fila_de_otros(tenant_id, record_id).encode()).hexdigest()


def _tandas(valores: list[Any]) -> list[list[Any]]:
    return [valores[i : i + _TANDA] for i in range(0, len(valores), _TANDA)]


async def analizar(
    session: AsyncSession, tenant_id: uuid.UUID, *, para_aplicar: bool = False
) -> Resumen:
    """Las filas PENDING con la huella del lote, huérfanas o con respaldo.

    Con ``para_aplicar`` las filas se toman ``FOR UPDATE``: un ``reclassify`` o un
    ``link-product`` en curso termina antes, y la fila que resolvió ya no es PENDING.
    """
    q = select(
        UnclassifiedRecord.id,
        UnclassifiedRecord.suggested_entity,
        UnclassifiedRecord.context_label,
    ).where(
        UnclassifiedRecord.tenant_id == tenant_id,
        UnclassifiedRecord.status == UNCLASSIFIED_STATUS_PENDING,
    )
    if para_aplicar:
        q = q.with_for_update()
    filas = (await session.execute(q)).all()
    resumen = Resumen(tenant_id=tenant_id)
    if not filas:
        return resumen

    por_huella = {_huella_de(tenant_id, f.id): f for f in filas}
    huellas: list[Huella] = []
    for tanda in _tandas(list(por_huella)):
        for fp in (
            await session.execute(
                select(OperationFingerprint).where(
                    OperationFingerprint.tenant_id == tenant_id,
                    OperationFingerprint.fingerprint.in_(tanda),
                )
            )
        ).scalars():
            fila = por_huella[fp.fingerprint]
            huellas.append(
                Huella(
                    record_id=fila.id,
                    fingerprint_id=fp.id,
                    fingerprint=fp.fingerprint,
                    action_type=fp.action_type,
                    executed_at=fp.executed_at,
                    sugerida=fila.suggested_entity,
                    motivo=fila.context_label,
                )
            )
    if not huellas:
        return resumen

    # La operación viva que nació de la fila (de a una o del lote) lleva su
    # referencia. Una anulada no respalda: la fila vuelve a estar por resolver.
    refs = [unclassified_row_ref(h.record_id) for h in huellas]
    respaldadas: set[str] = set()
    for modelo in (SaleEntry, ExpenseEntry):
        for tanda in _tandas(refs):
            respaldadas.update(
                str(r)
                for r in (
                    await session.execute(
                        select(modelo.source_row_ref).where(
                            modelo.tenant_id == tenant_id,
                            modelo.source_row_ref.in_(tanda),
                            modelo.voided_at.is_(None),
                        )
                    )
                ).scalars()
            )
    for h in sorted(huellas, key=lambda h: str(h.record_id)):
        if unclassified_row_ref(h.record_id) in respaldadas:
            resumen.con_respaldo.append(h)
        else:
            resumen.huerfanas.append(h)
    return resumen


async def reparar(session: AsyncSession, tenant_id: uuid.UUID, *, creado_por: str) -> Resumen:
    """Borra las huellas huérfanas del negocio, en la transacción del llamador."""
    # Los candados del lote, en su orden: una corrida de «Importar sugeridos» en
    # paralelo espera (o esto espera a que termine) en vez de cruzarse.
    await maintenance_lock_service.acquire_write_lock_shared(session, tenant_id)
    await maintenance_lock_service.acquire_import_lock(session, tenant_id)
    resumen = await analizar(session, tenant_id, para_aplicar=True)
    if not resumen.huerfanas:
        return resumen
    ids = [h.fingerprint_id for h in resumen.huerfanas]
    for tanda in _tandas(ids):
        await session.execute(
            delete(OperationFingerprint).where(
                OperationFingerprint.tenant_id == tenant_id,
                OperationFingerprint.id.in_(tanda),
            )
        )
    detalle: dict[str, Any] = {
        "creado_por": creado_por,
        "huellas": [
            {
                "record_id": str(h.record_id),
                "fingerprint": h.fingerprint,
                "action_type": h.action_type,
                "executed_at": h.executed_at.isoformat() if h.executed_at else None,
            }
            for h in resumen.huerfanas
        ],
        "con_respaldo": [str(h.record_id) for h in resumen.con_respaldo],
    }
    run = DataRepairRun(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        repair_type=REPAIR_TYPE,
        status="APPLIED",
        dry_run=False,
        candidates_found=len(resumen.huerfanas) + len(resumen.con_respaldo),
        details_json=detalle,
        completed_at=datetime.now(UTC),
    )
    session.add(run)
    session.add(
        DecisionAuditLog(
            tenant_id=tenant_id,
            decision_type="OTHERS_FINGERPRINTS_CLEANED",
            decision_data={
                "run_id": str(run.id),
                "borradas": len(resumen.huerfanas),
                "con_respaldo": len(resumen.con_respaldo),
            },
            triggered_by=creado_por,
            created_at=datetime.now(UTC),
        )
    )
    await session.flush()
    resumen.run_id = run.id
    return resumen


async def revertir(session: AsyncSession, run_id: uuid.UUID) -> int:
    """Vuelve a poner las huellas de la corrida que no estén. Devuelve cuántas."""
    run = await session.get(DataRepairRun, run_id, with_for_update=True)
    if run is None or run.repair_type != REPAIR_TYPE or run.tenant_id is None:
        raise ValueError(f"{run_id} no es una corrida de {REPAIR_TYPE}.")
    if run.status != "APPLIED":
        raise ValueError(f"La corrida {run_id} está {run.status}: no se revierte dos veces.")
    tenant_id = run.tenant_id
    await maintenance_lock_service.acquire_write_lock_shared(session, tenant_id)
    await maintenance_lock_service.acquire_import_lock(session, tenant_id)
    huellas = list((run.details_json or {}).get("huellas") or [])
    presentes: set[str] = set()
    for tanda in _tandas([h["fingerprint"] for h in huellas]):
        presentes.update(
            (
                await session.execute(
                    select(OperationFingerprint.fingerprint).where(
                        OperationFingerprint.tenant_id == tenant_id,
                        OperationFingerprint.fingerprint.in_(tanda),
                    )
                )
            ).scalars()
        )
    repuestas = 0
    for h in huellas:
        if h["fingerprint"] in presentes:
            continue
        fp = OperationFingerprint(
            tenant_id=tenant_id, fingerprint=h["fingerprint"], action_type=h["action_type"]
        )
        if h.get("executed_at"):
            fp.executed_at = datetime.fromisoformat(h["executed_at"])
        session.add(fp)
        presentes.add(h["fingerprint"])
        repuestas += 1
    run.status = "REVERTED"
    session.add(
        DecisionAuditLog(
            tenant_id=tenant_id,
            decision_type="OTHERS_FINGERPRINTS_RESTORED",
            decision_data={"run_id": str(run.id), "repuestas": repuestas},
            triggered_by=str((run.details_json or {}).get("creado_por") or REPAIR_TYPE),
            created_at=datetime.now(UTC),
        )
    )
    await session.flush()
    return repuestas
