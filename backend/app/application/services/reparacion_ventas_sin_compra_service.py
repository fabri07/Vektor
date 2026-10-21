"""B·5g: las ventas históricas sin compra previa pasan a «Otros» (con ``--apply``).

Desde B·5e ninguna venta nueva entra sin producto o sin una compra o stock
inicial con costo a su fecha. Las que ya estaban cargadas siguen ahí: la
relectura no las vuelve a juzgar a propósito (sería una reparación automática).
Esto es esa reparación, explícita y reversible, que autoriza el dueño:

- **analizar** (sólo lectura): por negocio, las ventas vivas sin producto o sin
  respaldo, por mes y por motivo. No toca nada.
- **reparar(aplicar=True)**: cada una se anula (``void_reason =
  REPAIR_SALE_WITHOUT_PURCHASE``), su descuento de stock se devuelve, y su fila
  queda en «Otros» con el motivo, para resolverla con la compra cargada. Todo en
  una transacción, con una ``DataRepairRun`` que guarda cada par venta↔fila.
- **revertir(run_id)**: restaura las ventas cuya fila de «Otros» sigue PENDING
  (y vuelve a aplicar su descuento); las que el usuario ya resolvió o descartó
  quedan como están, con el motivo.

Quedan afuera las ventas del POS: la plata y el turno son reales y no se mueven
desde un script; lo que les falta se ve en su tarea de «Otros» (B·5e-2).
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import stock_service
from app.application.services.respaldo_service import (
    MOTIVO_VENTA_SIN_COMPRA,
    MOTIVO_VENTA_SIN_PRODUCTO,
    primeros_dias_de_fuente,
    respaldada,
)
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.pos_operation import PosOperationLine
from app.persistence.models.product import Product
from app.persistence.models.repair import DataRepairRun
from app.persistence.models.transaction import SaleEntry
from app.persistence.models.unclassified_record import (
    UNCLASSIFIED_STATUS_PENDING,
    UnclassifiedRecord,
)

REPAIR_TYPE: Final = "B5G_VENTA_SIN_COMPRA_A_OTROS"
VOID_REASON: Final = "REPAIR_SALE_WITHOUT_PURCHASE"


@dataclass
class Candidata:
    venta: SaleEntry
    motivo: str
    producto: str | None


@dataclass
class Resumen:
    tenant_id: uuid.UUID
    candidatas: list[Candidata] = field(default_factory=list)
    run_id: uuid.UUID | None = None

    @property
    def monto(self) -> Decimal:
        return sum((c.venta.amount for c in self.candidatas), Decimal("0"))

    def por_motivo(self) -> Counter[str]:
        return Counter(
            MOTIVO_VENTA_SIN_PRODUCTO if c.producto is None else MOTIVO_VENTA_SIN_COMPRA
            for c in self.candidatas
        )

    def por_mes(self) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for c in self.candidatas:
            mes = c.venta.transaction_date.strftime("%Y-%m")
            out[mes] = out.get(mes, Decimal("0")) + c.venta.amount
        return dict(sorted(out.items()))


async def analizar(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    desde: date | None = None,
    hasta: date | None = None,
) -> Resumen:
    """Las ventas vivas del negocio sin producto o sin compra previa. SELECT-only."""
    q = select(SaleEntry).where(
        SaleEntry.tenant_id == tenant_id,
        SaleEntry.voided_at.is_(None),
        ~SaleEntry.id.in_(
            select(PosOperationLine.sale_entry_id).where(
                PosOperationLine.tenant_id == tenant_id,
                PosOperationLine.sale_entry_id.is_not(None),
            )
        ),
    )
    ventas = list((await session.execute(q.order_by(SaleEntry.transaction_date))).scalars())
    ventas = [
        v
        for v in ventas
        if (desde is None or v.transaction_date.date() >= desde)
        and (hasta is None or v.transaction_date.date() <= hasta)
    ]
    pids = {v.product_id for v in ventas if v.product_id is not None}
    primeros = await primeros_dias_de_fuente(session, tenant_id, pids)
    nombres: dict[uuid.UUID, str] = {}
    if pids:
        nombres = {
            fila[0]: fila[1]
            for fila in (
                await session.execute(
                    select(Product.id, Product.name).where(
                        Product.tenant_id == tenant_id, Product.id.in_(pids)
                    )
                )
            ).all()
        }
    resumen = Resumen(tenant_id=tenant_id)
    for v in ventas:
        if v.product_id is None:
            resumen.candidatas.append(
                Candidata(
                    v,
                    f"{MOTIVO_VENTA_SIN_PRODUCTO}: elegí qué producto se vendió "
                    "(sin producto no hay compra previa ni costo)",
                    None,
                )
            )
        elif not respaldada(primeros.get(v.product_id), v.transaction_date.date()):
            nombre = nombres.get(v.product_id, "el producto")
            resumen.candidatas.append(
                Candidata(
                    v,
                    f"{MOTIVO_VENTA_SIN_COMPRA}: cargá la compra o el stock inicial de {nombre}",
                    nombre,
                )
            )
    return resumen


async def reparar(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    desde: date | None = None,
    hasta: date | None = None,
    creado_por: str = "script:ventas_sin_compra_a_otros",
) -> Resumen:
    """Aplica: anula, devuelve el stock y deja la fila en «Otros». Sin commit."""
    resumen = await analizar(session, tenant_id, desde, hasta)
    run = DataRepairRun(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        repair_type=REPAIR_TYPE,
        status="APPLIED",
        dry_run=False,
        candidates_found=len(resumen.candidatas),
        sales_detected=len(resumen.candidatas),
        sales_voided=len(resumen.candidatas),
        details_json={},
    )
    session.add(run)
    ahora = datetime.now(UTC)
    pares: list[dict[str, Any]] = []
    for c in resumen.candidatas:
        v = c.venta
        movimiento = await stock_service._live_sale_movement(v.id, tenant_id, session)
        if movimiento is not None:
            await stock_service.void_movement(movimiento, session)
        v.voided_at = ahora
        v.void_reason = VOID_REASON
        fila = {
            "fecha": v.transaction_date.date().isoformat(),
            "monto": str(v.amount),
            "cantidad": v.quantity,
            "producto": c.producto,
            "medio_de_pago": v.payment_method,
            "detalle": v.notes,
            "sale_id": str(v.id),
        }
        registro = UnclassifiedRecord(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            source="reanalysis",
            uploaded_file_id=v.source_upload_id,
            context_label=c.motivo[:200],
            row_data={k: val for k, val in fila.items() if val not in (None, "")},
            suggested_entity="sale",
            status=UNCLASSIFIED_STATUS_PENDING,
        )
        session.add(registro)
        pares.append(
            {
                "sale_id": str(v.id),
                "record_id": str(registro.id),
                "movement_id": str(movimiento.id) if movimiento is not None else None,
            }
        )
    run.details_json = {
        "repair_type": REPAIR_TYPE,
        "pares": pares,
        "desde": desde.isoformat() if desde else None,
        "hasta": hasta.isoformat() if hasta else None,
        "monto": str(resumen.monto),
        "creado_por": creado_por,
    }
    await session.flush()
    resumen.run_id = run.id
    return resumen


@dataclass
class Reversion:
    restauradas: list[str] = field(default_factory=list)
    resueltas_por_el_usuario: list[str] = field(default_factory=list)


async def revertir(session: AsyncSession, run_id: uuid.UUID) -> Reversion:
    """Restaura las ventas cuya fila de «Otros» sigue PENDING. Sin commit."""
    run = await session.get(DataRepairRun, run_id)
    if run is None or run.repair_type != REPAIR_TYPE or run.tenant_id is None:
        raise ValueError(f"No es una corrida de {REPAIR_TYPE}: {run_id}")
    res = Reversion()
    for par in (run.details_json or {}).get("pares") or []:
        registro = await session.get(UnclassifiedRecord, uuid.UUID(par["record_id"]))
        venta = await session.get(SaleEntry, uuid.UUID(par["sale_id"]))
        if (
            registro is None
            or venta is None
            or registro.status != UNCLASSIFIED_STATUS_PENDING
            or venta.void_reason != VOID_REASON
        ):
            res.resueltas_por_el_usuario.append(par["sale_id"])
            continue
        venta.voided_at = None
        venta.void_reason = None
        if par.get("movement_id"):
            mov = await session.get(InventoryMovement, uuid.UUID(par["movement_id"]))
            if mov is not None and mov.voided_at is not None:
                await stock_service.unvoid_movement(mov, session)
        await session.delete(registro)
        res.restauradas.append(par["sale_id"])
    run.status = "REVERTED"
    await session.flush()
    return res


__all__ = [
    "REPAIR_TYPE",
    "VOID_REASON",
    "Candidata",
    "Resumen",
    "Reversion",
    "analizar",
    "reparar",
    "revertir",
]
