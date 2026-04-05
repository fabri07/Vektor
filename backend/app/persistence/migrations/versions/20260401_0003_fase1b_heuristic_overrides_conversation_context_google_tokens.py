"""fase1b_heuristic_overrides_conversation_context_google_tokens

Revision ID: 20260401_0003
Revises: 20260401_0001
Create Date: 2026-04-01 00:00:00.000000

Tablas nuevas:
  - business_heuristic_overrides
  - agent_conversation_context

Columnas nuevas:
  - business_profiles.heuristics_version
  - users.google_access_token_encrypted
  - users.google_refresh_token_encrypted
  - users.google_scopes
  - users.google_connected_at

RLS habilitado en las dos tablas nuevas (policy por tenant_id).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260401_0003"
down_revision: Union[str, None] = "20260401_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 1. CREATE TABLE business_heuristic_overrides ──────────────────────────
    op.create_table(
        "business_heuristic_overrides",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("param_key", sa.Text(), nullable=False),
        sa.Column(
            "param_value",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )
    op.create_index(
        "idx_heuristic_overrides_tenant",
        "business_heuristic_overrides",
        ["tenant_id"],
    )

    # ── 2. CREATE TABLE agent_conversation_context ────────────────────────────
    op.create_table(
        "agent_conversation_context",
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "turns",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column(
            "total_tokens",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )
    op.create_index(
        "idx_conversation_context_tenant",
        "agent_conversation_context",
        ["tenant_id"],
    )
    op.create_index(
        "idx_conversation_context_user",
        "agent_conversation_context",
        ["user_id"],
    )

    # ── 3. ALTER TABLE business_profiles — heuristics_version ─────────────────
    op.execute(
        "ALTER TABLE business_profiles "
        "ADD COLUMN IF NOT EXISTS heuristics_version TEXT NOT NULL DEFAULT 'v1'"
    )

    # ── 4. ALTER TABLE users — Google OAuth columns ───────────────────────────
    op.execute(
        "ALTER TABLE users "
        "ADD COLUMN IF NOT EXISTS google_access_token_encrypted TEXT"
    )
    op.execute(
        "ALTER TABLE users "
        "ADD COLUMN IF NOT EXISTS google_refresh_token_encrypted TEXT"
    )
    op.execute(
        "ALTER TABLE users "
        "ADD COLUMN IF NOT EXISTS google_scopes TEXT[]"
    )
    op.execute(
        "ALTER TABLE users "
        "ADD COLUMN IF NOT EXISTS google_connected_at TIMESTAMPTZ"
    )

    # ── 5. RLS en las tablas nuevas ───────────────────────────────────────────
    op.execute("ALTER TABLE business_heuristic_overrides ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY business_heuristic_overrides_tenant
        ON business_heuristic_overrides
        USING (tenant_id = current_setting('app.current_tenant_id', TRUE)::uuid)
        """
    )

    op.execute("ALTER TABLE agent_conversation_context ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY agent_conversation_context_tenant
        ON agent_conversation_context
        USING (tenant_id = current_setting('app.current_tenant_id', TRUE)::uuid)
        """
    )


def downgrade() -> None:
    # Deshabilitar RLS y eliminar políticas
    op.execute(
        "DROP POLICY IF EXISTS agent_conversation_context_tenant "
        "ON agent_conversation_context"
    )
    op.execute("ALTER TABLE agent_conversation_context DISABLE ROW LEVEL SECURITY")

    op.execute(
        "DROP POLICY IF EXISTS business_heuristic_overrides_tenant "
        "ON business_heuristic_overrides"
    )
    op.execute("ALTER TABLE business_heuristic_overrides DISABLE ROW LEVEL SECURITY")

    # Eliminar columnas de users
    op.drop_column("users", "google_connected_at")
    op.drop_column("users", "google_scopes")
    op.drop_column("users", "google_refresh_token_encrypted")
    op.drop_column("users", "google_access_token_encrypted")

    # Eliminar columna de business_profiles
    op.drop_column("business_profiles", "heuristics_version")

    # Eliminar tablas nuevas
    op.drop_index("idx_conversation_context_user", table_name="agent_conversation_context")
    op.drop_index("idx_conversation_context_tenant", table_name="agent_conversation_context")
    op.drop_table("agent_conversation_context")

    op.drop_index("idx_heuristic_overrides_tenant", table_name="business_heuristic_overrides")
    op.drop_table("business_heuristic_overrides")
