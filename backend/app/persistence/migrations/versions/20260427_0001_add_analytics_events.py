"""add analytics_events table

Revision ID: 20260427_0001
Revises: 20260426_0001
Create Date: 2026-04-27
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "20260427_0001"
down_revision: Union[str, None] = "20260426_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "analytics_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("vertical_code", sa.String(50), nullable=False),
        sa.Column("score_total", sa.Integer, nullable=True),
        sa.Column("score_cash", sa.Integer, nullable=True),
        sa.Column("score_margin", sa.Integer, nullable=True),
        sa.Column("score_stock", sa.Integer, nullable=True),
        sa.Column("score_supplier", sa.Integer, nullable=True),
        sa.Column("margin_ratio", sa.Float, nullable=True),
        sa.Column("cash_ratio", sa.Float, nullable=True),
        sa.Column("supplier_count", sa.Integer, nullable=True),
        sa.Column("product_count", sa.Integer, nullable=True),
        sa.Column("low_stock_pct", sa.Float, nullable=True),
        sa.Column("data_completeness", sa.Float, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )
    op.create_index("ix_analytics_events_vertical_code", "analytics_events", ["vertical_code"])
    op.create_index("ix_analytics_events_created_at", "analytics_events", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_analytics_events_created_at", table_name="analytics_events")
    op.drop_index("ix_analytics_events_vertical_code", table_name="analytics_events")
    op.drop_table("analytics_events")
