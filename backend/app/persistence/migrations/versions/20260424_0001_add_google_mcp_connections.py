"""add google_mcp_connections table

Revision ID: 20260424_0001
Revises: 20260421_0002
Create Date: 2026-04-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "20260424_0001"
down_revision: str | None = "20260421_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "google_mcp_connections",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(32), nullable=False, server_default="DISCONNECTED"),
        # JSONB en Postgres, JSON en SQLite (tests)
        sa.Column("scopes_granted", JSONB, nullable=False, server_default="[]"),
        sa.Column("state_token", sa.Text, nullable=True),
        sa.Column("connected_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "user_id",
            name="uq_google_mcp_connection_tenant_user",
        ),
    )
    op.create_index("ix_google_mcp_connections_tenant_id", "google_mcp_connections", ["tenant_id"])
    op.create_index("ix_google_mcp_connections_user_id", "google_mcp_connections", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_google_mcp_connections_user_id", table_name="google_mcp_connections")
    op.drop_index("ix_google_mcp_connections_tenant_id", table_name="google_mcp_connections")
    op.drop_table("google_mcp_connections")
