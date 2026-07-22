"""ORM models: inventory_balances, inventory_movements."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    ColumnElement,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class InventoryBalance(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Current stock level per product per tenant."""

    __tablename__ = "inventory_balances"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("products.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    current_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reserved_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        # F5-B — un solo balance por (tenant, producto). No es parcial: acá NO hay
        # concepto de baja, un segundo balance para el mismo producto es siempre un
        # bug (el dedup de F3 los fusiona antes de que este índice pueda crearse).
        # Sostiene el get-or-create de balances: los dos sitios que los crean
        # (``stock_service._get_or_create_balance`` e
        # ``ingestion_import_service._apply_purchase_to_stock``) resuelven la carrera
        # con ``guarded_savepoint`` + ``BALANCE_CONFLICT`` y re-consultan al perder.
        # NO hay ningún ON CONFLICT sobre esta tabla.
        Index("uq_inventory_balances_tenant_product", "tenant_id", "product_id", unique=True),
    )

    def __repr__(self) -> str:
        return (
            f"<InventoryBalance product={self.product_id}"
            f" qty={self.current_qty} reserved={self.reserved_qty}>"
        )


class InventoryMovement(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Immutable log of every stock change. Insert-only."""

    __tablename__ = "inventory_movements"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("products.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # FASE 3 (Proveedores): proveedor de una compra. NULL si no es compra o no se
    # informó. SET NULL al borrar el proveedor: el movimiento histórico se conserva.
    supplier_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("suppliers.id", ondelete="SET NULL"),
        nullable=True,
    )
    # movement_type: sale | purchase | loss | adjustment | return
    movement_type: Mapped[str] = mapped_column(String(30), nullable=False)
    # positive = stock increase, negative = stock decrease
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    source_event_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Origen del movimiento (spec: app/application/services/inventory_movement_origin.py).
    # source_type: semántica canónica (purchase_import, catalog_initial_stock, ...).
    # source_upload_id / source_row_ref: archivo y fila que lo originaron (traza + reversa
    #   del reread). source_row_hash: identidad lógica estable (idempotencia, no depende
    #   del orden del Excel). voided_at: soft-delete (dedup, reread) — un movimiento
    #   voidado NO cuenta para stock ni para "comprado".
    # Invariante de DB (migración 20260728_0001): un movement_type='adjustment' VIVO
    # (voided_at IS NULL) EXIGE source_type no nulo (CHECK
    # ck_inventory_movements_adjustment_source_type) — un ajuste sin procedencia
    # rastreable no puede reconciliarse ni auditarse. Los voideados quedan exentos
    # (ya no afectan stock). No aplica (todavía) a purchase/sale/loss.
    source_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    source_upload_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("uploaded_files.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    source_row_ref: Mapped[str | None] = mapped_column(String(100), nullable=True)
    source_row_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Fecha de NEGOCIO del movimiento (cuándo ocurrió la compra/venta/ajuste en el
    # mundo real), NO cuándo se insertó (created_at = fecha de carga del archivo).
    # Nullable: filas legacy no la tienen — SIEMPRE leer COALESCE(occurred_at,
    # created_at). Incidente 2026-07 ("don pedro"): el dedup agrupó por
    # date(created_at) y voideó compras reales de meses distintos cargadas el
    # mismo día.
    # NOTA: es wall-clock de negocio AR etiquetado UTC (la fecha del día es correcta
    # para bucketing por día); NO convertir con astimezone para cómputos sub-día.
    occurred_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:
        return (
            f"<InventoryMovement product={self.product_id}"
            f" type={self.movement_type!r} qty={self.qty:+d}>"
        )


def movement_business_time(movement: Any = InventoryMovement) -> ColumnElement[datetime]:
    """Expresión SQL de la fecha de NEGOCIO del movimiento (invariante 2d):
    ``COALESCE(occurred_at, created_at)``. NUNCA usar ``created_at`` solo — filas
    legacy sin ``occurred_at`` deben caer a la fecha de carga, y agregaciones/orden
    por "cuándo pasó" deben usar la de negocio (incidente don pedro 2026-07).

    Acepta la clase o un ``aliased(InventoryMovement)`` para usarla dentro de window
    functions o subqueries sobre el mismo modelo. Devuelve una expresión ligada a
    la instancia/alias que se pasa (no una constante a nivel módulo), justamente
    para que sirva con ``aliased()``.

    Nota: hay un gemelo Python en ``product_dedup_service._movement_time`` que aplica
    la MISMA regla sobre ``MovementRow`` (objetos en memoria del replay de dedup);
    son capas distintas (expresión SQL vs valor Python) y no se pueden unificar en
    una sola función, pero codifican la misma invariante.
    """
    return func.coalesce(movement.occurred_at, movement.created_at)
