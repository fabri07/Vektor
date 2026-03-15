"""Add momentum fields to weekly_score_history and business_profiles.

Revision ID: 20260315_0001
Revises: 20260310_0001
Create Date: 2026-03-15 10:00:00

Changes:
  weekly_score_history  — delta (Numeric 6,2), trend_label (String 20)
  business_profiles     — weekly_report_day (Integer), weekly_report_hour (Integer)
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260315_0001"
down_revision: Union[str, None] = "20260310_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── weekly_score_history ──────────────────────────────────────────────────
    op.add_column(
        "weekly_score_history",
        sa.Column("delta", sa.Numeric(6, 2), nullable=True),
    )
    op.add_column(
        "weekly_score_history",
        sa.Column("trend_label", sa.String(20), nullable=True),
    )

    # ── business_profiles ─────────────────────────────────────────────────────
    op.add_column(
        "business_profiles",
        sa.Column("weekly_report_day", sa.Integer(), nullable=True),
    )
    op.add_column(
        "business_profiles",
        sa.Column("weekly_report_hour", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("business_profiles", "weekly_report_hour")
    op.drop_column("business_profiles", "weekly_report_day")
    op.drop_column("weekly_score_history", "trend_label")
    op.drop_column("weekly_score_history", "delta")
