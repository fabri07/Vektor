"""Pagos de deuda a proveedores (`supplier_payments`).

Revision ID: 20261116_0002
Revises: 20261116_0001

Un pago a un proveedor por una compra a cuenta corriente no es un gasto ni una compra:
saca plata de la caja y baja la deuda con el proveedor, sin tocar el costo ni el margen
(la compra fiada ya contó). Importe siempre positivo, no se borra —se anula con quién y
por qué—, y la procedencia admite un archivo (borrarlo lo anula).

Aditiva e idempotente (convención E8c; el lock de ``migrations/env.py`` serializa a la
API y al worker). Comprueba PRESENCIA, no forma. Ningún código viejo la lee: el
despliegue en cualquier orden es seguro.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261116_0002"
down_revision = "20261116_0001"
branch_labels = None
depends_on = None

_TABLA = "supplier_payments"
_INDICES = {
    "ix_supplier_payments_tenant_date": ["tenant_id", "occurred_on"],
    "ix_supplier_payments_tenant_supplier": ["tenant_id", "supplier_id"],
    "ix_supplier_payments_tenant_id": ["tenant_id"],
}
_METODOS = "'cash', 'debit_card', 'credit_card', 'transfer', 'qr', 'other'"


def _insp() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def upgrade() -> None:
    bind = op.get_bind()
    es_pg = bind.dialect.name == "postgresql"
    uuid_t: sa.types.TypeEngine[object] = (
        postgresql.UUID(as_uuid=True) if es_pg else sa.String(36)
    )

    if not _insp().has_table(_TABLA):
        op.create_table(
            _TABLA,
            sa.Column("id", uuid_t, primary_key=True),
            sa.Column(
                "tenant_id",
                uuid_t,
                sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "supplier_id",
                uuid_t,
                sa.ForeignKey("suppliers.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("occurred_on", sa.Date(), nullable=False),
            sa.Column("amount_ars", sa.Numeric(14, 2), nullable=False),
            sa.Column("payment_method", sa.String(30), nullable=False),
            sa.Column("notes", sa.String(1000), nullable=True),
            sa.Column("source_type", sa.String(30), nullable=False, server_default="manual"),
            sa.Column(
                "source_upload_id",
                uuid_t,
                sa.ForeignKey("uploaded_files.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("source_ref", sa.String(100), nullable=True),
            sa.Column(
                "created_by_user_id",
                uuid_t,
                sa.ForeignKey("users.user_id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "voided_by_user_id",
                uuid_t,
                sa.ForeignKey("users.user_id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("void_reason", sa.String(500), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.CheckConstraint("amount_ars > 0", name="ck_supplier_payments_amount_positive"),
            sa.CheckConstraint(
                f"payment_method IN ({_METODOS})", name="ck_supplier_payments_method"
            ),
            sa.CheckConstraint(
                "source_type IN ('manual', 'archivo')", name="ck_supplier_payments_source_type"
            ),
        )

    existentes = {ix["name"] for ix in _insp().get_indexes(_TABLA)}
    for nombre, columnas in _INDICES.items():
        if nombre not in existentes:
            op.create_index(nombre, _TABLA, columnas)


def downgrade() -> None:
    if _insp().has_table(_TABLA):
        op.drop_table(_TABLA)
