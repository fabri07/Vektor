"""Salida pendiente de stock de una venta de caja (B11 del POS).

Revision ID: 20260927_0002
Revises: 20260927_0001

Una venta cobrada sin conexión puede llegar cuando ya no hay stock. Rechazarla
no deshace nada: la plata entró y la mercadería salió. La venta entra completa y
queda esta deuda, que un humano resuelve.

Guarda la **salida pendiente** (la cantidad completa de la línea), no el
faltante: si había 3 y se vendieron 5, quedan 5 unidades pendientes de salir, y
un ajuste por 2 dejaría el stock mal.

Aditiva e idempotente (convención E8c; el lock de ``migrations/env.py``
serializa a la API y al worker). Comprueba PRESENCIA, no forma.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260927_0002"
down_revision = "20260927_0001"
branch_labels = None
depends_on = None

_TABLA = "pos_stock_discrepancies"
_INDICE_ESTADO = "ix_pos_stock_discrepancies_tenant_status"


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
                "operation_id",
                uuid_t,
                sa.ForeignKey("pos_operations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "line_id",
                uuid_t,
                sa.ForeignKey("pos_operation_lines.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "sale_entry_id",
                uuid_t,
                sa.ForeignKey("sales_entries.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "product_id",
                uuid_t,
                sa.ForeignKey("products.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("pending_units", sa.Integer(), nullable=False),
            sa.Column("stock_at_capture", sa.Integer(), nullable=False),
            sa.Column(
                "status", sa.String(20), nullable=False, server_default="OPEN"
            ),
            sa.Column("resolution", sa.String(30), nullable=True),
            sa.Column("reason", sa.String(300), nullable=True),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "resolved_by_user_id",
                uuid_t,
                sa.ForeignKey("users.user_id", ondelete="SET NULL"),
                nullable=True,
            ),
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
            sa.UniqueConstraint("line_id", name="uq_pos_stock_discrepancies_linea"),
            sa.CheckConstraint(
                "pending_units > 0", name="ck_pos_stock_discrepancies_pendiente"
            ),
            sa.CheckConstraint(
                "status IN ('OPEN','RESOLVED')", name="ck_pos_stock_discrepancies_status"
            ),
        )
        op.create_index("ix_pos_stock_discrepancies_tenant_id", _TABLA, ["tenant_id"])
        op.create_index("ix_pos_stock_discrepancies_operation_id", _TABLA, ["operation_id"])

    indices = {ix["name"] for ix in _insp().get_indexes(_TABLA)}
    if _INDICE_ESTADO not in indices:
        op.create_index(_INDICE_ESTADO, _TABLA, ["tenant_id", "status"])


def downgrade() -> None:
    if _insp().has_table(_TABLA):
        op.drop_table(_TABLA)
