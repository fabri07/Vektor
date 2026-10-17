"""Costeo retroactivo de las ventas anteriores a M·2a (M·2c).

Las ventas creadas antes de M·2a no tienen snapshot de costo (NULL = sin
costear). Éste las costea con la MISMA regla que el costeo al confirmar
(``persistence/costeo_de_ventas.costear_ventas``: la fuente elegible al día de
cada venta y la base comparable del régimen), con dos diferencias:

- **Nunca confirmado.** Se costea DESPUÉS de la venta, con lo que hoy se sabe de
  las compras de entonces: es una estimación, nunca margen realizado
  (``cost_confirmed = false`` y ``cost_reason`` empieza con
  ``MOTIVO_RETROACTIVO``). El resumen dice cuántas habrían salido confirmadas, sólo
  como dato.
- **Unidad.** Para un producto fraccionado (factor ≠ 1), sólo se confía en la
  cantidad de las ventas que garantizan unidades base: las del POS (tienen
  ``pos_operation_lines``) y las importadas de un archivo (``source_upload_id``).
  El resto (``POST /sales``, ``bulk``, chat) queda ``sin_costo`` con motivo, igual
  que en vivo.

**Dry-run por defecto:** el llamador revierte la transacción. Con ``aplicar`` deja
un ``data_repair_runs`` (``M2C_COSTEO_RETROACTIVO``) y un ``data_repair_items``
``UPDATE_SALE`` por venta con el snapshot escrito: ``revertir`` vuelve a NULL las
ventas cuyo snapshot sigue siendo el que escribió esa corrida. Sólo toca ventas
SIN snapshot: reejecutar no duplica ni pisa nada.

``tenant_id`` lo pasa el script (nunca hay JWT acá); cada consulta filtra por él.
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.domain.margen import COSTO_DESACTUALIZADO_DIAS
from app.persistence.costeo_de_ventas import costear_ventas, snapshots_sin_escribir
from app.persistence.models.pos_operation import PosOperationLine
from app.persistence.models.product import Product
from app.persistence.models.repair import DataRepairItem, DataRepairRun
from app.persistence.models.transaction import SaleEntry

REPAIR_TYPE: Final = "M2C_COSTEO_RETROACTIVO"
#: M·3-4: ``--solo-fecha`` completa ``cost_source_date`` de ventas ya costeadas.
REPAIR_TYPE_FECHA: Final = "M34_FECHA_DEL_COSTO"
MOTIVO_RETROACTIVO: Final = "costeado después de la venta (M·2c): estimación"
LOTE: Final = 500

_COLUMNAS: Final = (
    "cost_unit_ars", "cost_ars", "cost_confirmed", "cost_origin", "cost_reason",
    "cost_snapshot_version",
)


@dataclass
class Resumen:
    tenant_id: uuid.UUID
    ventas: int = 0
    por_origen: Counter[str] = field(default_factory=Counter)
    #: Cuántas habrían salido confirmadas en vivo (dato; quedan como estimación).
    habrian_sido_confirmadas: int = 0
    sin_costear_por_error: int = 0
    ingreso_total: Decimal = Decimal("0")
    ingreso_con_costo: Decimal = Decimal("0")
    costo_total: Decimal = Decimal("0")
    run_id: uuid.UUID | None = None

    @property
    def cobertura_pct(self) -> Decimal | None:
        if self.ingreso_total <= 0:
            return None
        return (self.ingreso_con_costo / self.ingreso_total * 100).quantize(Decimal("0.1"))

    @property
    def margen_estimado_pct(self) -> Decimal | None:
        if self.ingreso_con_costo <= 0:
            return None
        ganancia = self.ingreso_con_costo - self.costo_total
        return (ganancia / self.ingreso_con_costo * 100).quantize(Decimal("0.1"))


def _snapshot(venta: SaleEntry) -> dict[str, Any]:
    return {
        c: (str(v) if isinstance(v, Decimal) else v)
        for c in _COLUMNAS
        for v in [getattr(venta, c)]
    }


async def ventas_sin_costear(session: AsyncSession, tenant_id: uuid.UUID) -> list[uuid.UUID]:
    filas = await session.execute(
        select(SaleEntry.id)
        .where(
            SaleEntry.tenant_id == tenant_id,
            SaleEntry.product_id.is_not(None),
            SaleEntry.voided_at.is_(None),
            SaleEntry.cost_snapshot_version.is_(None),
        )
        .order_by(SaleEntry.transaction_date, SaleEntry.id)
    )
    return list(filas.scalars())


async def _unidad_dudosa(
    session: AsyncSession, tenant_id: uuid.UUID, ids: list[uuid.UUID]
) -> set[uuid.UUID]:
    """Ventas de producto fraccionado cuya cantidad no está garantizada en
    unidades base (ni POS ni archivo)."""
    con_pos = select(PosOperationLine.sale_entry_id).where(
        PosOperationLine.sale_entry_id.in_(ids)
    )
    filas = await session.execute(
        select(SaleEntry.id)
        .join(Product, Product.id == SaleEntry.product_id)
        .where(
            SaleEntry.tenant_id == tenant_id,
            SaleEntry.id.in_(ids),
            Product.base_units_per_sale_unit != 1,
            SaleEntry.source_upload_id.is_(None),
            SaleEntry.id.not_in(con_pos),
        )
    )
    return set(filas.scalars())


async def costear_historico(
    session: AsyncSession, tenant_id: uuid.UUID, *, aplicar: bool
) -> Resumen:
    """Costea las ventas sin snapshot del tenant. Sin ``aplicar`` es un ensayo: el
    llamador hace ``rollback``."""
    resumen = Resumen(tenant_id=tenant_id)
    ids = await ventas_sin_costear(session, tenant_id)
    run: DataRepairRun | None = None
    if aplicar and ids:
        run = DataRepairRun(
            tenant_id=tenant_id,
            repair_type=REPAIR_TYPE,
            status="RUNNING",
            dry_run=False,
            candidates_found=len(ids),
        )
        session.add(run)
        await session.flush()
        resumen.run_id = run.id

    for i in range(0, len(ids), LOTE):
        # Bloquea el lote y vuelve a filtrar: una venta que se costeó, anuló o
        # perdió el producto desde que se listó (camino en vivo) no se pisa.
        lote = list(
            (
                await session.execute(
                    select(SaleEntry.id)
                    .where(
                        SaleEntry.id.in_(ids[i : i + LOTE]),
                        SaleEntry.tenant_id == tenant_id,
                        SaleEntry.product_id.is_not(None),
                        SaleEntry.voided_at.is_(None),
                        SaleEntry.cost_snapshot_version.is_(None),
                    )
                    .with_for_update()
                )
            ).scalars()
        )
        if not lote:
            continue
        dudosas = await _unidad_dudosa(session, tenant_id, lote)
        pendientes = dict.fromkeys(lote, True)

        def _costear(s: Session, pendientes: dict[uuid.UUID, bool] = pendientes,
                     dudosas: set[uuid.UUID] = dudosas) -> None:
            costear_ventas(s, pendientes, dudosas)

        await session.run_sync(_costear)
        ventas = (
            await session.execute(select(SaleEntry).where(SaleEntry.id.in_(lote)))
        ).scalars()
        for venta in ventas:
            resumen.ventas += 1
            resumen.ingreso_total += venta.amount
            if venta.cost_snapshot_version is None:
                resumen.sin_costear_por_error += 1
                continue
            if venta.cost_confirmed:
                resumen.habrian_sido_confirmadas += 1
            venta.cost_confirmed = False
            motivo = MOTIVO_RETROACTIVO + (f"; {venta.cost_reason}" if venta.cost_reason else "")
            venta.cost_reason = motivo[:200]
            resumen.por_origen[venta.cost_origin or "?"] += 1
            if venta.cost_ars is not None:
                resumen.ingreso_con_costo += venta.amount
                resumen.costo_total += venta.cost_ars
            if run is not None:
                session.add(
                    DataRepairItem(
                        run_id=run.id,
                        tenant_id=tenant_id,
                        sale_entry_id=venta.id,
                        product_id=venta.product_id,
                        action="UPDATE_SALE",
                        before_json=dict.fromkeys(_COLUMNAS),
                        after_json=_snapshot(venta),
                        confidence="LOW",
                    )
                )
        await session.flush()

    if run is not None:
        run.status = "APPLIED"
        run.sales_detected = resumen.ventas
        run.completed_at = datetime.now(UTC)
        run.details_json = {
            "por_origen": dict(resumen.por_origen),
            "habrian_sido_confirmadas": resumen.habrian_sido_confirmadas,
            "sin_costear_por_error": resumen.sin_costear_por_error,
            "ingreso_total": str(resumen.ingreso_total),
            "ingreso_con_costo": str(resumen.ingreso_con_costo),
            "costo_total": str(resumen.costo_total),
        }
        await session.flush()
    return resumen


async def revertir(session: AsyncSession, run_id: uuid.UUID) -> tuple[int, int]:
    """Vuelve a NULL las ventas de la corrida cuyo snapshot sigue siendo el que
    escribió. Devuelve (revertidas, salteadas porque cambiaron después)."""
    run = await session.get(DataRepairRun, run_id)
    if run is not None and run.repair_type == REPAIR_TYPE_FECHA:
        return await _revertir_fechas(session, run)
    if run is None or run.repair_type != REPAIR_TYPE:
        raise ValueError(f"No es una corrida de {REPAIR_TYPE}: {run_id}")
    if run.status == "REVERTED":
        return 0, 0
    items = list(
        (
            await session.execute(select(DataRepairItem).where(DataRepairItem.run_id == run_id))
        ).scalars()
    )
    ventas: dict[uuid.UUID, SaleEntry] = {}
    for i in range(0, len(items), LOTE):
        ids = [it.sale_entry_id for it in items[i : i + LOTE] if it.sale_entry_id is not None]
        for v in (
            await session.execute(select(SaleEntry).where(SaleEntry.id.in_(ids)).with_for_update())
        ).scalars():
            ventas[v.id] = v
    revertidas = salteadas = 0
    for item in items:
        venta = ventas.get(item.sale_entry_id) if item.sale_entry_id else None
        if (
            venta is None
            or venta.tenant_id != run.tenant_id
            or _snapshot(venta) != item.after_json
        ):
            salteadas += 1
            continue
        for columna in _COLUMNAS:
            setattr(venta, columna, None)
        # M·3-4: la fecha de la fuente se escribió con ese snapshot (no está en
        # ``_COLUMNAS`` para no romper la comparación de corridas anteriores).
        venta.cost_source_date = None
        revertidas += 1
    run.status = "REVERTED"
    await session.flush()
    return revertidas, salteadas


# ─────────────────────────────────────────────────────────────────────────────
# M·3-4 — ``--solo-fecha``: la fecha de la fuente de ventas YA costeadas
# ─────────────────────────────────────────────────────────────────────────────
#
# Las ventas costeadas antes de M·3-4 no tienen ``cost_source_date``. Se vuelve a
# elegir la fuente a la fecha de cada venta con la MISMA regla del costeo y se
# escribe su fecha SÓLO si esa fuente da el mismo costo y el mismo origen que los
# congelados: si no coinciden (se cargó otra compra de esa fecha después, se
# corrigió un costo), la venta queda con antigüedad desconocida. Nunca revalúa:
# no toca ``cost_*``. Un costo cargado sin fecha queda NULL.


@dataclass
class ResumenFecha:
    tenant_id: uuid.UUID
    ventas: int = 0
    fechadas: int = 0
    #: La fuente elegida hoy no da el costo congelado: quedan sin fecha.
    no_coinciden: int = 0
    #: La fuente es un costo cargado sin fecha: antigüedad desconocida.
    fuente_sin_fecha: int = 0
    #: Fechadas con más de 45 días a la fecha de la venta.
    desactualizadas: int = 0
    run_id: uuid.UUID | None = None


async def ventas_sin_fecha_de_costo(
    session: AsyncSession, tenant_id: uuid.UUID
) -> list[uuid.UUID]:
    filas = await session.execute(
        select(SaleEntry.id)
        .where(
            SaleEntry.tenant_id == tenant_id,
            SaleEntry.product_id.is_not(None),
            SaleEntry.voided_at.is_(None),
            SaleEntry.cost_snapshot_version.is_not(None),
            SaleEntry.cost_unit_ars.is_not(None),
            SaleEntry.cost_source_date.is_(None),
        )
        .order_by(SaleEntry.transaction_date, SaleEntry.id)
    )
    return list(filas.scalars())


async def fechar_costos(
    session: AsyncSession, tenant_id: uuid.UUID, *, aplicar: bool
) -> ResumenFecha:
    """Completa ``cost_source_date`` donde la fuente coincide. Sin ``aplicar`` es
    un ensayo: el llamador hace ``rollback``."""
    resumen = ResumenFecha(tenant_id=tenant_id)
    ids = await ventas_sin_fecha_de_costo(session, tenant_id)
    # La corrida nace con la primera venta que se fecha: re-ejecutar sobre ventas
    # que nunca coinciden no deja corridas vacías en ``data_repair_runs``.
    run: DataRepairRun | None = None

    for i in range(0, len(ids), LOTE):
        ventas = list(
            (
                await session.execute(
                    select(SaleEntry)
                    .where(
                        SaleEntry.id.in_(ids[i : i + LOTE]),
                        SaleEntry.tenant_id == tenant_id,
                        SaleEntry.voided_at.is_(None),
                        SaleEntry.cost_unit_ars.is_not(None),
                        SaleEntry.cost_source_date.is_(None),
                    )
                    .with_for_update()
                )
            ).scalars()
        )
        if not ventas:
            continue

        def _elegir(s: Session, ventas: list[SaleEntry] = ventas) -> list[Any]:
            return snapshots_sin_escribir(s, tenant_id, ventas, set())

        for venta, snapshot in await session.run_sync(_elegir):
            resumen.ventas += 1
            if (
                snapshot.costo_unitario != venta.cost_unit_ars
                or snapshot.origen != venta.cost_origin
            ):
                resumen.no_coinciden += 1
                continue
            if snapshot.fecha_fuente is None:
                resumen.fuente_sin_fecha += 1
                continue
            if aplicar and run is None:
                run = DataRepairRun(
                    tenant_id=tenant_id, repair_type=REPAIR_TYPE_FECHA, status="RUNNING",
                    dry_run=False, candidates_found=len(ids),
                )
                session.add(run)
                await session.flush()
                resumen.run_id = run.id
            venta.cost_source_date = snapshot.fecha_fuente
            resumen.fechadas += 1
            dia = venta.transaction_date
            dia_venta = dia.date() if isinstance(dia, datetime) else dia
            if (dia_venta - snapshot.fecha_fuente).days > COSTO_DESACTUALIZADO_DIAS:
                resumen.desactualizadas += 1
            if run is not None:
                session.add(
                    DataRepairItem(
                        run_id=run.id, tenant_id=tenant_id, sale_entry_id=venta.id,
                        product_id=venta.product_id, action="UPDATE_SALE",
                        before_json={"cost_source_date": None},
                        after_json={"cost_source_date": snapshot.fecha_fuente.isoformat()},
                        confidence="HIGH",
                    )
                )
        await session.flush()

    if run is not None:
        run.status = "APPLIED"
        run.sales_detected = resumen.ventas
        run.completed_at = datetime.now(UTC)
        run.details_json = {
            "fechadas": resumen.fechadas, "no_coinciden": resumen.no_coinciden,
            "fuente_sin_fecha": resumen.fuente_sin_fecha,
            "desactualizadas": resumen.desactualizadas,
        }
        await session.flush()
    return resumen


async def _revertir_fechas(session: AsyncSession, run: DataRepairRun) -> tuple[int, int]:
    if run.status == "REVERTED":
        return 0, 0
    items = list(
        (
            await session.execute(select(DataRepairItem).where(DataRepairItem.run_id == run.id))
        ).scalars()
    )
    revertidas = salteadas = 0
    for i in range(0, len(items), LOTE):
        tramo = items[i : i + LOTE]
        ventas = {
            v.id: v
            for v in (
                await session.execute(
                    select(SaleEntry)
                    .where(SaleEntry.id.in_([it.sale_entry_id for it in tramo]))
                    .with_for_update()
                )
            ).scalars()
        }
        for item in tramo:
            venta = ventas.get(item.sale_entry_id) if item.sale_entry_id else None
            escrita = (item.after_json or {}).get("cost_source_date")
            if (
                venta is None
                or venta.tenant_id != run.tenant_id
                or venta.cost_source_date is None
                or venta.cost_source_date.isoformat() != escrita
            ):
                salteadas += 1
                continue
            venta.cost_source_date = None
            revertidas += 1
    run.status = "REVERTED"
    await session.flush()
    return revertidas, salteadas
