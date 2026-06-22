"""supplier_id en inventory_movements (proveedor de la compra)

Revision ID: 20260720_0004
Revises: 20260720_0003
Create Date: 2026-07-20

Contexto
--------
Migración ADDITIVE — reforma de Proveedores (Fase 3). La cantidad y el costo
unitario de una compra viven en ``inventory_movements`` (no en ``expense_entries``),
pero la tabla no sabía DE QUÉ proveedor. Se agrega:

  - ``inventory_movements.supplier_id`` (UUID, nullable) + FK ``suppliers.id``
    ``ondelete=SET NULL`` (el movimiento histórico se conserva si se borra el
    proveedor). NULL = compra sin proveedor / movimiento que no es de compra.
  - Índice ``(tenant_id, supplier_id)`` para la query "productos comprados a un
    proveedor".

Nullable/additive — no reescribe datos. ``downgrade`` simétrico.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "20260720_0004"
down_revision = "20260720_0003"
branch_labels = None
depends_on = None

_FK = "fk_inventory_movements_supplier_id"
_IX = "ix_inventory_movements_tenant_supplier"


def upgrade() -> None:
    op.add_column(
        "inventory_movements",
        sa.Column("supplier_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        _FK,
        "inventory_movements",
        "suppliers",
        ["supplier_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        _IX, "inventory_movements", ["tenant_id", "supplier_id"]
    )


def downgrade() -> None:
    op.drop_index(_IX, table_name="inventory_movements")
    op.drop_constraint(_FK, "inventory_movements", type_="foreignkey")
    op.drop_column("inventory_movements", "supplier_id")
