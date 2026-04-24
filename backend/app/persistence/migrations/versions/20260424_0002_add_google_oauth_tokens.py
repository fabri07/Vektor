"""add google_oauth_tokens table for mcp server

Revision ID: 20260424_0002
Revises: 20260424_0001
Create Date: 2026-04-24
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "20260424_0002"
down_revision: Union[str, None] = "20260424_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "google_oauth_tokens",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False, server_default="google"),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("state_token", sa.Text(), nullable=True),
        sa.Column("state_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("scopes_requested", JSONB, nullable=False, server_default="[]"),
        sa.Column("scopes_granted", JSONB, nullable=False, server_default="[]"),
        sa.Column("access_token_encrypted", sa.Text(), nullable=True),
        sa.Column("refresh_token_encrypted", sa.Text(), nullable=True),
        sa.Column("token_type", sa.String(32), nullable=True),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
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
            "provider",
            name="uq_google_oauth_tokens_tenant_user_provider",
        ),
    )
    op.create_index("ix_google_oauth_tokens_tenant_id", "google_oauth_tokens", ["tenant_id"])
    op.create_index("ix_google_oauth_tokens_user_id", "google_oauth_tokens", ["user_id"])
    op.create_index("ix_google_oauth_tokens_state_token", "google_oauth_tokens", ["state_token"])


def downgrade() -> None:
    op.drop_index("ix_google_oauth_tokens_state_token", table_name="google_oauth_tokens")
    op.drop_index("ix_google_oauth_tokens_user_id", table_name="google_oauth_tokens")
    op.drop_index("ix_google_oauth_tokens_tenant_id", table_name="google_oauth_tokens")
    op.drop_table("google_oauth_tokens")
