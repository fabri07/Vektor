"""Sprint 20: tabla cash_closes (cierre de caja diario / arqueo)

Revision ID: 20260617_0001
Revises: 20260616_0001
Create Date: 2026-06-17

Contexto
--------
Migración ADDITIVE: nueva tabla `cash_closes` para el arqueo diario.
Guarda el esperado (calculado server-side: ventas por método con neteo de
efectivo), el contado (ingresado por el usuario) y la diferencia. Un solo
cierre por (tenant, fecha) — unique constraint. Se suma al informe semanal.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260617_0001"
down_revision = "20260616_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cash_closes",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("close_date", sa.Date(), nullable=False),
        sa.Column("expected_total_ars", sa.Numeric(14, 2), nullable=False),
        sa.Column("counted_total_ars", sa.Numeric(14, 2), nullable=False),
        sa.Column("difference_ars", sa.Numeric(14, 2), nullable=False),
        sa.Column(
            "breakdown_by_method",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("closed_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["closed_by_user_id"], ["users.user_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "close_date", name="uq_cash_closes_tenant_date"),
    )
    op.create_index(
        "ix_cash_closes_tenant_date", "cash_closes", ["tenant_id", "close_date"]
    )
    op.create_index(
        op.f("ix_cash_closes_tenant_id"), "cash_closes", ["tenant_id"]
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_cash_closes_tenant_id"), table_name="cash_closes")
    op.drop_index("ix_cash_closes_tenant_date", table_name="cash_closes")
    op.drop_table("cash_closes")
