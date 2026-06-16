"""social_metrics (Marketing — Fase 4)

Revision ID: 20260718_0004
Revises: 20260718_0003
Create Date: 2026-07-18

Contexto
--------
Migración ADDITIVE y reversible. Crea la tabla `social_metrics`: métricas de redes
sociales cargadas MANUALMENTE por el tenant (sin OAuth ahora). Alimenta el
dashboard unificado de redes + el cruce ads vs ventas.

  - `tenant_id` FK CASCADE a `tenants.tenant_id` (multi-tenant).
  - `platform` ("instagram"|"facebook"|"tiktok"|"other") + `metric_date` (Date).
  - `followers`/`reach`/`engagement` (Integer default 0).
  - `ads_spend_ars` Numeric(14,2) default 0.
  - Índice `ix_social_metrics_tenant`.

No requiere backfill: tabla nueva.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "20260718_0004"
down_revision = "20260718_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "social_metrics",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("platform", sa.String(20), nullable=False),
        sa.Column("metric_date", sa.Date(), nullable=False),
        sa.Column("followers", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reach", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("engagement", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "ads_spend_ars",
            sa.Numeric(14, 2),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_social_metrics_tenant", "social_metrics", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_social_metrics_tenant", table_name="social_metrics")
    op.drop_table("social_metrics")
