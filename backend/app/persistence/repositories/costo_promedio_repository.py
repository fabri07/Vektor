"""Precarga de la historia de cada producto para el costo promedio (M·P2a).

No reusa la precarga del chequeo temporal ni la del costo de referencia: una no
trae costos ni ids, la otra deja afuera las compras sin costo. Acá va todo lo que
``domain/costo_promedio`` necesita, en pocas consultas por tenant:

- **Movimientos vivos** del ledger con su clase (``clasificar``), costo e id. El
  stock sale del LEDGER (decisión del dueño, 2026-10-13), así que coincide con el
  stock del sistema.
- **Catálogo reparado por M·R:** los movimientos cuyo gasto anuló una corrida
  ``MR_STOCK_INICIAL_APERTURA`` aplicada (el id viaja en el item).
- **Aproximado:** el producto tiene ventas vivas sin un movimiento de stock vivo
  que las descuente (una planilla importada sin descontar stock). Las ventas de
  caja con salida pendiente (B11) no cuentan: todavía no descontaron y van a
  hacerlo.
- **Stock fuera del ledger:** el ledger no reproduce ``stock_units``. Las salidas
  (``sale``/``loss``) clampan el stock en 0 pero guardan la cantidad entera, así
  que se simula el stock del sistema en el orden de CARGA con ese clamp. Si cierra
  sólo con el clamp, no hay stock por fuera: el producto queda ``aproximado``
  (el dominio no reproduce el clamp: arrastra el faltante).

**Base comparable (M·P4b).** Cada costo entra en la base de la reposición de la
venta (``domain/costo_promedio.costo_comparable``): se lee el contrato de cada
movimiento y el régimen del tenant. Un RI sin neto de IVA confirmado deja esas
unidades sin costo, con su motivo.

**Fechas.** ``occurred_at`` ya es hora argentina rotulada UTC: se le saca la zona.
``created_at`` es un instante UTC real (de 21 a 24 hora AR ya es el día
siguiente): se lleva a hora argentina. Lo mismo para la carga (desempate).

``tenant_id`` lo pasa el llamador; cada consulta filtra por él.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.business_time import a_hora_de_negocio
from app.domain.costo_promedio import (
    AJUSTE_MANUAL,
    MOTIVO_SIN_COSTO,
    MOTIVO_SIN_COSTO_RI,
    ContratoDelMovimiento,
    Evento,
    clasificar,
    costo_comparable,
    es_ri,
)
from app.persistence.models.business import BusinessProfile
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.repair import DataRepairItem, DataRepairRun
from app.persistence.models.transaction import SaleEntry

#: Mismo literal que ``reparacion_stock_inicial_service.REPAIR_TYPE`` (un test
#: los ata): importar el servicio arrastraría la relectura entera.
REPAIR_TYPE_MR: Final = "MR_STOCK_INICIAL_APERTURA"
#: Las clases cuyo costo cuenta para el promedio (``domain/costo_promedio.aplicar``).
_CON_COSTO: Final = frozenset({"apertura", "compra", "entrada", "ancla"})
#: ``stock_service.sale_source_event_id``.
PREFIJO_VENTA: Final = "sale:"


@dataclass
class HistoriaProducto:
    product_id: uuid.UUID
    eventos: list[Evento] = field(default_factory=list)
    stock_fuera_del_ledger: bool = False
    aproximado: bool = False
    #: El motivo de las unidades sin costo (un RI: ``MOTIVO_SIN_COSTO_RI``).
    motivo_sin_costo: str = MOTIVO_SIN_COSTO


def _hora_de_negocio_del_ledger(occurred_at: datetime | None, created_at: datetime) -> datetime:
    """``COALESCE(occurred_at, created_at)`` llevado a hora argentina sin zona."""
    if occurred_at is not None:
        return occurred_at.replace(tzinfo=None)
    return _carga_en_hora_ar(created_at)


def _carga_en_hora_ar(created_at: datetime) -> datetime:
    # SQLite devuelve ``created_at`` sin zona: también es UTC.
    instante = created_at if created_at.tzinfo is not None else created_at.replace(tzinfo=UTC)
    return a_hora_de_negocio(instante)


async def _movimientos_reparados_por_mr(
    session: AsyncSession, tenant_id: uuid.UUID
) -> set[uuid.UUID]:
    filas = await session.execute(
        select(DataRepairItem.before_json)
        .join(DataRepairRun, DataRepairRun.id == DataRepairItem.run_id)
        .where(
            DataRepairRun.tenant_id == tenant_id,
            DataRepairRun.repair_type == REPAIR_TYPE_MR,
            DataRepairRun.status == "APPLIED",
            DataRepairRun.dry_run.is_(False),
        )
    )
    ids: set[uuid.UUID] = set()
    for antes in filas.scalars():
        mov = (antes or {}).get("movement_id")
        if mov:
            ids.add(uuid.UUID(str(mov)))
    return ids


async def _ventas_sin_movimiento(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    descontadas: set[str],
    filtro: list[uuid.UUID] | None,
) -> set[uuid.UUID]:
    """Productos con alguna venta viva que no descontó stock (ni está pendiente)."""
    from app.application.services.pos_discrepancy_service import (  # noqa: PLC0415
        ventas_con_salida_pendiente,
    )

    consulta = select(SaleEntry.id, SaleEntry.product_id).where(
        SaleEntry.tenant_id == tenant_id,
        SaleEntry.product_id.is_not(None),
        SaleEntry.voided_at.is_(None),
        # Una venta sin cantidad positiva no tiene nada que descontar.
        SaleEntry.quantity > 0,
    )
    if filtro is not None:
        consulta = consulta.where(SaleEntry.product_id.in_(filtro))
    pendientes = await ventas_con_salida_pendiente(session, tenant_id, filtro)
    productos: set[uuid.UUID] = set()
    for venta_id, pid in (await session.execute(consulta)).tuples():
        if pid is None or venta_id in pendientes:
            continue
        if f"{PREFIJO_VENTA}{venta_id}" not in descontadas:
            productos.add(pid)
    return productos


async def cargar_historias(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    product_ids: Iterable[uuid.UUID] | None = None,
) -> dict[uuid.UUID, HistoriaProducto]:
    """La historia de cada producto del tenant (o de los pedidos), lista para
    ``domain/costo_promedio.proyectar``."""
    filtro = None if product_ids is None else list(dict.fromkeys(product_ids))
    consulta_productos = select(Product.id, Product.stock_units).where(
        Product.tenant_id == tenant_id
    )
    if filtro is not None:
        consulta_productos = consulta_productos.where(Product.id.in_(filtro))
    stock: dict[uuid.UUID, int] = {
        pid: int(su or 0) for pid, su in (await session.execute(consulta_productos)).tuples()
    }
    regimen = (
        await session.execute(
            select(BusinessProfile.fiscal_condition).where(BusinessProfile.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    motivo = MOTIVO_SIN_COSTO_RI if es_ri(regimen) else MOTIVO_SIN_COSTO
    historias = {pid: HistoriaProducto(pid, motivo_sin_costo=motivo) for pid in stock}
    if not historias:
        return historias

    reparados = await _movimientos_reparados_por_mr(session, tenant_id)
    m = InventoryMovement
    consulta = select(
        m.id,
        m.product_id,
        m.movement_type,
        m.source_type,
        m.qty,
        m.unit_cost,
        m.unit_cost_final,
        m.occurred_at,
        m.created_at,
        m.source_event_id,
        m.shipping_treatment,
        m.tax_treatment,
        m.tax_rate,
        m.tax_amount,
        m.tax_recoverable,
    ).where(m.tenant_id == tenant_id, m.voided_at.is_(None))
    if filtro is not None:
        consulta = consulta.where(m.product_id.in_(filtro))

    suma_ledger: dict[uuid.UUID, int] = defaultdict(int)
    cargas: dict[uuid.UUID, list[tuple[datetime, str, int, bool]]] = defaultdict(list)
    descontadas: set[str] = set()
    # Core directo sobre la conexión: un producto con decenas de miles de
    # movimientos no paga el armado de filas del ORM (medido: ~0,5 s en 50.000).
    conexion = await session.connection()
    for fila in (await conexion.execute(consulta)).tuples():
        (
            mid, pid, tipo, origen, qty, costo, costo_final, occurred_at, created_at, evento,
            envio, impuesto, tasa, monto_iva, recuperable,
        ) = fila
        historia = historias.get(pid)
        if historia is None:
            continue
        suma_ledger[pid] += int(qty)
        cargas[pid].append((created_at, str(mid), int(qty), tipo in ("sale", "loss")))
        if tipo == "sale" and evento:
            descontadas.add(str(evento))
        clase = clasificar(tipo, origen, int(qty), catalogo_reparado=mid in reparados)
        if clase is None:
            continue
        crudo = costo_final if costo_final is not None else costo
        precio = None if crudo is None else Decimal(crudo)
        if clase in _CON_COSTO:
            # Sólo las clases que traen mercadería con precio: una venta no paga
            # el armado de la base.
            precio = costo_comparable(
                precio,
                clase,
                ContratoDelMovimiento(envio, impuesto, tasa, monto_iva, recuperable),
                regimen,
            )
        historia.eventos.append(
            Evento(
                fecha=_hora_de_negocio_del_ledger(occurred_at, created_at),
                clase=clase,
                unidades=abs(int(qty)),
                costo=precio,
                # Sólo desempata dentro del mismo día y la hora: no hace falta llevarlo
                # a hora argentina (el corrimiento no cambia el orden) y ahorra la
                # conversión de zona en cada fila.
                creado=created_at.replace(tzinfo=None),
                id=mid,
                movement_id=mid,
                # M·P2c: el ajuste manual con precio de compra declarado
                # (contrato escrito) promedia con ese costo.
                costo_declarado=(origen == AJUSTE_MANUAL and costo_final is not None),
            )
        )

    for pid in await _ventas_sin_movimiento(session, tenant_id, descontadas, filtro):
        if pid in historias:
            historias[pid].aproximado = True
    for pid, historia in historias.items():
        real = stock[pid]
        if suma_ledger.get(pid, 0) == real:
            continue
        if _stock_con_clamp(cargas.get(pid, [])) == real:
            historia.aproximado = True
        else:
            historia.stock_fuera_del_ledger = True
    return historias


def _stock_con_clamp(cargas: list[tuple[datetime, str, int, bool]]) -> int:
    """El stock que dejó el sistema: en orden de carga, las salidas no lo bajan de 0."""
    s = 0
    for _, _, qty, es_salida in sorted(cargas, key=lambda c: (_utc(c[0]), c[1])):
        s = max(0, s + qty) if es_salida and qty < 0 else s + qty
    return s


def _utc(momento: datetime) -> datetime:
    return momento if momento.tzinfo is not None else momento.replace(tzinfo=UTC)
