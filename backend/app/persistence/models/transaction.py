"""ORM models: sales_entries, expense_entries, products."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db.base import PGJSONB, Base, TimestampMixin, UUIDPrimaryKeyMixin


class SaleEntry(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "sales_entries"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("products.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Cliente opcional vinculado a la venta (Fase 2). NULL = venta sin cliente
    # informado / histórica. SET NULL al borrar el cliente: la venta se conserva.
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="SET NULL"),
        nullable=True,
    )
    # B1: archivo de origen de una fila importada (NULL = manual / histórico).
    # Da señal de origen-import al detector de duplicados; SET NULL al borrar el
    # archivo (la transacción se conserva).
    source_upload_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("uploaded_files.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Precio REALMENTE vendido en esta transacción (NULL = no informado). No es
    # `Product.sale_price_ars`, que es el precio vigente configurado: el precio
    # histórico cambia por descuento, fecha, canal o cliente y por eso vive en la
    # transacción. NUNCA se deriva de `amount / quantity` — en las filas
    # históricas no se sabe si el amount es unitario o total, y esa división daría
    # un número plausible pero inventado.
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    transaction_date: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    payment_method: Mapped[str] = mapped_column(String(30), nullable=False, default="cash")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    provenance: Mapped[str] = mapped_column(String(10), nullable=False, default="REAL")
    # Relectura de archivos: marca si esta fila importada fue editada a mano
    # (la re-importación no debe pisarla). `source_row_ref` ata el registro a su
    # fila de origen en el archivo para reconciliar al re-importar.
    has_user_edits: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
    source_row_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    custom_fields: Mapped[dict[str, Any]] = mapped_column(
        PGJSONB, nullable=False, server_default="'{}'::jsonb", default=dict
    )
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    void_reason: Mapped[str | None] = mapped_column(String(30), nullable=True)
    voided_by_repair_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint("provenance IN ('REAL', 'DEMO')", name="ck_sales_entries_provenance"),
        CheckConstraint(
            "void_reason IS NULL OR void_reason IN ("
            "'REPAIR_MISCLASSIFIED_IMPORT','USER_CANCELLED','DUPLICATE','MANUAL_ADMIN_VOID',"
            "'REREAD_REIMPORT')",
            name="ck_sales_entries_void_reason",
        ),
        CheckConstraint(
            "voided_at IS NULL OR void_reason IS NOT NULL",
            name="ck_sales_entries_void_consistency",
        ),
        Index("ix_sales_entries_tenant_provenance", "tenant_id", "provenance"),
        Index("ix_sales_entries_tenant_voided_at", "tenant_id", "voided_at"),
        # last_sold por producto (FactsProvider): max(transaction_date) GROUP BY
        # product_id sobre el histórico del tenant, en cada snapshot de métricas.
        Index("ix_sales_entries_tenant_product", "tenant_id", "product_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<SaleEntry tenant={self.tenant_id} amount={self.amount} date={self.transaction_date}>"  # noqa: E501
        )


class ExpenseEntry(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "expense_entries"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # FASE 3 (B1): vínculo opcional al producto del catálogo (compras de
    # mercadería/insumos). NULL si no se resolvió o es ambiguo. SET NULL al
    # borrar el producto: el gasto histórico se conserva.
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("products.id", ondelete="SET NULL"),
        nullable=True,
    )
    # FASE 3: vínculo opcional al proveedor del catálogo. NULL = gasto sin
    # proveedor informado / histórico. SET NULL al borrar el proveedor: el gasto
    # se conserva. Coexiste con `supplier_name` (texto libre, no se reemplaza).
    supplier_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("suppliers.id", ondelete="SET NULL"),
        nullable=True,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    # OPEX = gasto operativo; COGS = compra de mercadería (entra al stock).
    expense_type: Mapped[str] = mapped_column(
        String(10), nullable=False, server_default="OPEX", default="OPEX"
    )
    # B1: archivo de origen de una fila importada (NULL = manual / histórico).
    source_upload_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("uploaded_files.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    transaction_date: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    is_recurring: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    payment_method: Mapped[str] = mapped_column(String(30), nullable=False, default="transfer")
    supplier_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    provenance: Mapped[str] = mapped_column(String(10), nullable=False, default="REAL")
    # Relectura de archivos: marca si esta fila importada fue editada a mano
    # (la re-importación no debe pisarla). `source_row_ref` ata el registro a su
    # fila de origen en el archivo para reconciliar al re-importar.
    has_user_edits: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
    source_row_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    custom_fields: Mapped[dict[str, Any]] = mapped_column(
        PGJSONB, nullable=False, server_default="'{}'::jsonb", default=dict
    )
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    void_reason: Mapped[str | None] = mapped_column(String(30), nullable=True)
    voided_by_repair_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint("provenance IN ('REAL', 'DEMO')", name="ck_expense_entries_provenance"),
        CheckConstraint(
            "expense_type IN ('OPEX', 'COGS')", name="ck_expense_entries_expense_type"
        ),
        Index("ix_expense_entries_tenant_expense_type", "tenant_id", "expense_type"),
        CheckConstraint(
            "void_reason IS NULL OR void_reason IN ("
            "'REPAIR_MISCLASSIFIED_IMPORT','USER_CANCELLED','DUPLICATE','MANUAL_ADMIN_VOID',"
            "'REREAD_REIMPORT')",
            name="ck_expense_entries_void_reason",
        ),
        CheckConstraint(
            "voided_at IS NULL OR void_reason IS NOT NULL",
            name="ck_expense_entries_void_consistency",
        ),
        Index("ix_expense_entries_tenant_provenance", "tenant_id", "provenance"),
        Index("ix_expense_entries_tenant_voided_at", "tenant_id", "voided_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<ExpenseEntry tenant={self.tenant_id}"
            f" amount={self.amount} category={self.category!r}>"
        )
