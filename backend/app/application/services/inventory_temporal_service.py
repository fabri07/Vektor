"""Reconstrucción TEMPORAL de stock: reproduce los eventos de un producto por FECHA DE
NEGOCIO y detecta dónde el balance reconstruido cae por debajo de 0 — es decir, ventas
(mayormente importadas históricamente) que superan el stock reconstruible por fechas.

Es la versión temporal del chequeo agregado de ``inventory_integrity_service.py``: aquel
mira la MAGNITUD (``stock_units`` vs esperado all-time); éste mira la SECUENCIA (una venta
datada ANTES que la compra que la cubre). Son complementarios.

Puramente de lectura — NUNCA escribe ``stock_units`` ni ninguna otra columna (regla del
repo: nunca recomputar stock desde el ledger). Solo reporta y clasifica causa probable;
la corrección la decide un humano.

Reglas de clasificación de movimientos: IDÉNTICAS al chequeo agregado, para que el
invariante ``ending_balance == stock_esperado`` (del agregado) se cumpla:
- ancla = movimiento vivo ``source_type=catalog_initial_stock`` que NO es ``purchase``
  (típicamente un ``adjustment``): es el stock inicial conocido → se siembra como
  *opening*. Un ``purchase`` con ese source_type ES una compra real y va como evento.
- ``purchase`` (cualquier source_type) → evento +.
- ``adjustment`` con source_type tagueado (reconciliation/manual_adjustment) → evento ±.
- ``loss`` → evento + (la merma ya viene negativa en el ledger).
- ventas desde ``sales_entries.quantity`` → evento −.
- movimiento ``sale`` del ledger → se IGNORA (dedup: la venta se cuenta desde
  ``sales_entries``, la fuente de verdad).
- ``return`` o ``adjustment`` sin source_type tagueado → ledger complejo (legacy no
  auditable) → se saltea el producto (no arriesgar un falso positivo).

DECISIÓN DE ANCLAJE TEMPORAL (honesta): el ancla ``catalog_initial_stock`` es un SNAPSHOT
ABSOLUTO; su ``occurred_at`` suele ser la fecha de IMPORT del catálogo, no la fecha de
negocio del stock inicial. Confiamos en su VALOR (como opening) pero IGNORAMOS su fecha:
el stock inicial se considera presente desde el inicio de la línea de tiempo. Un replay
que anclara en la fecha del snapshot marcaría todas las ventas previas al import como
negativos espurios.

DESEMPATE INTRA-DÍA: muchos imports traen solo el DÍA (aunque la columna sea DATETIME) y
``occurred_at`` es wall-clock AR etiquetado UTC → válido solo para bucketing por día. En
fecha igual se procesan CRÉDITOS (compra/ajuste) ANTES que DÉBITOS (venta): así una compra
del mismo día que cubre la venta no genera un negativo espurio. Sesgo deliberado hacia
no-falso-positivo (a costa de no ver una sobreventa que se resuelve el mismo día).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.inventory_movement_origin import (
    MOVEMENT_CLASS_ANCHOR,
    MOVEMENT_CLASS_COMPLEX,
    MOVEMENT_CLASS_LOSS,
    MOVEMENT_CLASS_PURCHASE,
    MOVEMENT_CLASS_TAGGED_ADJUSTMENT,
    SOURCE_CATALOG_INITIAL_STOCK,
    classify_stock_movement,
)
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.transaction import SaleEntry

DECISION_TYPE_TEMPORAL = "HISTORICAL_STOCK_TEMPORAL_DIVERGENCE"

# Causa probable de la divergencia (best-effort).
CAUSE_PURCHASES_DATED_AFTER_SALES = "PURCHASES_DATED_AFTER_SALES"
CAUSE_NO_PURCHASES_OR_OVERSOLD = "NO_PURCHASES_OR_OVERSOLD"

# Piso absoluto: sólo se reporta si el path cae por debajo de -floor (evita ruido de
# 1-2 unidades por redondeos o coarse dates). Mismo espíritu que el chequeo agregado.
_DEFAULT_ABSOLUTE_FLOOR_UNITS = 5


@dataclass(frozen=True)
class _Event:
    """Un evento datado de la línea de tiempo. ``delta`` +crédito / −débito."""

    day: date
    delta: int
    kind_rank: int  # desempate determinista intra-día, mismo día y mismo signo


@dataclass(frozen=True)
class ReplayResult:
    ending_balance: int
    min_balance: int
    min_balance_at: date | None
    first_negative_at: date | None


@dataclass(frozen=True)
class TemporalDivergence:
    product_id: str
    product_name: str
    stock_units: int  # contexto; NO entra en la fórmula del replay
    ending_balance: int  # == stock_esperado del chequeo agregado (invariante)
    opening_anchor_qty: int
    min_balance: int
    min_balance_at: date | None
    first_negative_at: date | None
    total_purchases: int
    total_tagged_adjustments: int
    total_loss: int
    total_sales: int
    event_count: int
    cause: str

    def as_dict(self) -> dict[str, object]:
        """Serializable (fechas → ISO) para el dict de retorno / JSONB de auditoría."""
        return {
            "product_id": self.product_id,
            "product_name": self.product_name,
            "stock_units": self.stock_units,
            "ending_balance": self.ending_balance,
            "opening_anchor_qty": self.opening_anchor_qty,
            "min_balance": self.min_balance,
            "min_balance_at": self.min_balance_at.isoformat() if self.min_balance_at else None,
            "first_negative_at": (
                self.first_negative_at.isoformat() if self.first_negative_at else None
            ),
            "total_purchases": self.total_purchases,
            "total_tagged_adjustments": self.total_tagged_adjustments,
            "total_loss": self.total_loss,
            "total_sales": self.total_sales,
            "event_count": self.event_count,
            "cause": self.cause,
        }


@dataclass(frozen=True)
class TemporalScanResult:
    tenant_id: str
    checked: int
    divergences: list[TemporalDivergence]
    skipped_no_anchor: int
    skipped_complex_ledger: int
    absolute_floor_units: int

    def divergences_as_dicts(self) -> list[dict[str, object]]:
        return [d.as_dict() for d in self.divergences]


def replay_timeline(*, opening_anchor_qty: int, events: Sequence[_Event]) -> ReplayResult:
    """Corre el balance desde ``opening_anchor_qty`` aplicando los eventos por día.

    Orden: por día; MISMO día → créditos (``delta >= 0``) antes que débitos, luego
    ``kind_rank``. Devuelve el balance final, el mínimo alcanzado y las fechas del mínimo
    y del primer negativo. Función PURA (sin DB) — el corazón testeable del algoritmo.
    """
    ordered = sorted(events, key=lambda e: (e.day, 0 if e.delta >= 0 else 1, e.kind_rank))

    balance = opening_anchor_qty
    min_balance = opening_anchor_qty
    min_balance_at: date | None = None
    first_negative_at: date | None = None
    for event in ordered:
        balance += event.delta
        if balance < min_balance:
            min_balance = balance
            min_balance_at = event.day
        if balance < 0 and first_negative_at is None:
            first_negative_at = event.day
    return ReplayResult(
        ending_balance=balance,
        min_balance=min_balance,
        min_balance_at=min_balance_at,
        first_negative_at=first_negative_at,
    )


async def _candidate_products(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    product_ids: Sequence[uuid.UUID] | None,
) -> list[tuple[uuid.UUID, str, int]]:
    """Productos con ancla confiable (movimiento vivo ``catalog_initial_stock``).

    Mismo criterio que el chequeo agregado — la selección de candidatos ES la compuerta
    de ancla. Si ``product_ids`` viene dado, se restringe a ellos (los del set sin ancla
    quedan fuera y cuentan como ``skipped_no_anchor``).
    """
    stmt = (
        select(Product.id, Product.name, Product.stock_units)
        .distinct()
        .join(
            InventoryMovement,
            and_(
                InventoryMovement.tenant_id == Product.tenant_id,
                InventoryMovement.product_id == Product.id,
            ),
        )
        .where(
            Product.tenant_id == tenant_id,
            InventoryMovement.source_type == SOURCE_CATALOG_INITIAL_STOCK,
            InventoryMovement.voided_at.is_(None),
        )
    )
    if product_ids is not None:
        stmt = stmt.where(Product.id.in_(list(product_ids)))
    rows = (await session.execute(stmt)).all()
    return [(r[0], r[1], int(r[2])) for r in rows]


async def check_products_temporal_divergence(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    product_ids: Sequence[uuid.UUID] | None = None,
    absolute_floor_units: int = _DEFAULT_ABSOLUTE_FLOOR_UNITS,
) -> TemporalScanResult:
    """Escanea los productos con ancla y reporta divergencias TEMPORALES. No escribe nada.

    ``product_ids=None`` → todos los candidatos del tenant (script + job semanal).
    ``product_ids`` acotado → sólo esos (warning en confirm de import).
    """
    candidates = await _candidate_products(session, tenant_id, product_ids)

    # Denominador para skipped_no_anchor: cuántos productos del scope no tienen ancla.
    if product_ids is not None:
        scope_total = len({str(pid) for pid in product_ids})
    else:
        scope_total = int(
            (
                await session.execute(
                    select(func.count()).select_from(Product).where(Product.tenant_id == tenant_id)
                )
            ).scalar_one()
        )
    skipped_no_anchor = max(0, scope_total - len(candidates))
    skipped_complex_ledger = 0
    divergences: list[TemporalDivergence] = []

    for pid, pname, stock_units in candidates:
        mov_rows = (
            await session.execute(
                select(
                    InventoryMovement.movement_type,
                    InventoryMovement.source_type,
                    InventoryMovement.qty,
                    InventoryMovement.occurred_at,
                    InventoryMovement.created_at,
                ).where(
                    InventoryMovement.tenant_id == tenant_id,
                    InventoryMovement.product_id == pid,
                    InventoryMovement.voided_at.is_(None),
                )
            )
        ).all()

        opening = 0
        total_purchases = 0
        total_tagged_adjustments = 0
        total_loss = 0
        events: list[_Event] = []
        complex_ledger = False
        for movement_type, source_type, qty, occurred_at, created_at in mov_rows:
            qty = int(qty)
            # Fecha de NEGOCIO: occurred_at si está, si no la fecha de carga. Reducimos a
            # .date() para poder ordenar/comparar naive vs aware sin TypeError (bucketing
            # por día es lo único válido con occurred_at wall-clock AR etiquetado UTC).
            business_day = (occurred_at or created_at).date()
            # Misma clasificación que el chequeo agregado (invariante ending==esperado).
            movement_class = classify_stock_movement(movement_type, source_type)
            if movement_class == MOVEMENT_CLASS_PURCHASE:
                total_purchases += qty
                events.append(_Event(business_day, qty, kind_rank=0))
            elif movement_class == MOVEMENT_CLASS_ANCHOR:
                # Ancla: stock inicial conocido → opening, IGNORANDO su fecha.
                opening += qty
            elif movement_class == MOVEMENT_CLASS_TAGGED_ADJUSTMENT:
                total_tagged_adjustments += qty
                events.append(_Event(business_day, qty, kind_rank=1))
            elif movement_class == MOVEMENT_CLASS_LOSS:
                total_loss += qty  # ya viene negativo en el ledger
                events.append(_Event(business_day, qty, kind_rank=2))
            elif movement_class == MOVEMENT_CLASS_COMPLEX:
                complex_ledger = True
            # MOVEMENT_CLASS_IGNORE_SALE: dedup, la venta se cuenta desde sales_entries.

        if complex_ledger:
            skipped_complex_ledger += 1
            continue

        sale_rows = (
            await session.execute(
                select(SaleEntry.quantity, SaleEntry.transaction_date).where(
                    SaleEntry.tenant_id == tenant_id,
                    SaleEntry.product_id == pid,
                    SaleEntry.voided_at.is_(None),
                )
            )
        ).all()
        total_sales = 0
        for quantity, transaction_date in sale_rows:
            quantity = int(quantity)
            total_sales += quantity
            events.append(_Event(transaction_date.date(), -quantity, kind_rank=3))

        result = replay_timeline(opening_anchor_qty=opening, events=events)

        # Sólo es divergencia si el path cayó por debajo del piso negativo.
        if result.min_balance >= -absolute_floor_units:
            continue

        cause = (
            CAUSE_PURCHASES_DATED_AFTER_SALES
            if result.ending_balance >= 0
            else CAUSE_NO_PURCHASES_OR_OVERSOLD
        )
        divergences.append(
            TemporalDivergence(
                product_id=str(pid),
                product_name=pname,
                stock_units=stock_units,
                ending_balance=result.ending_balance,
                opening_anchor_qty=opening,
                min_balance=result.min_balance,
                min_balance_at=result.min_balance_at,
                first_negative_at=result.first_negative_at,
                total_purchases=total_purchases,
                total_tagged_adjustments=total_tagged_adjustments,
                total_loss=total_loss,
                total_sales=total_sales,
                event_count=len(events),
                cause=cause,
            )
        )

    return TemporalScanResult(
        tenant_id=str(tenant_id),
        checked=len(candidates),
        divergences=divergences,
        skipped_no_anchor=skipped_no_anchor,
        skipped_complex_ledger=skipped_complex_ledger,
        absolute_floor_units=absolute_floor_units,
    )
