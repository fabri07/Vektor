"""Restore chat context and heuristic override schema.

Revision ID: 20260401_0003
Revises: 20260401_0002
Create Date: 2026-04-01

Compatibility migration restored after the original Sprint 1/3 Google
Workspace migrations were removed from the repository. Only the schema
still used by the current codebase is recreated here:
  - business_heuristic_overrides
  - agent_conversation_context
  - business_profiles.heuristics_version
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260401_0003"
down_revision: Union[str, None] = "20260401_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS business_heuristic_overrides (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
            param_key TEXT NOT NULL,
            param_value JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_heuristic_overrides_tenant
        ON business_heuristic_overrides (tenant_id)
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_conversation_context (
            conversation_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
            user_id UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            turns JSONB NOT NULL DEFAULT '[]'::jsonb,
            summary TEXT NULL,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_conversation_context_tenant
        ON agent_conversation_context (tenant_id)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_conversation_context_user
        ON agent_conversation_context (user_id)
        """
    )

    op.execute(
        """
        ALTER TABLE business_profiles
        ADD COLUMN IF NOT EXISTS heuristics_version TEXT NOT NULL DEFAULT 'v1'
        """
    )

    if dialect == "postgresql":
        op.execute("ALTER TABLE business_heuristic_overrides ENABLE ROW LEVEL SECURITY")
        op.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1
                    FROM pg_policies
                    WHERE schemaname = 'public'
                      AND tablename = 'business_heuristic_overrides'
                      AND policyname = 'business_heuristic_overrides_tenant'
                ) THEN
                    CREATE POLICY business_heuristic_overrides_tenant
                    ON business_heuristic_overrides
                    USING (tenant_id = current_setting('app.current_tenant_id', TRUE)::uuid);
                END IF;
            END $$;
            """
        )

        op.execute("ALTER TABLE agent_conversation_context ENABLE ROW LEVEL SECURITY")
        op.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1
                    FROM pg_policies
                    WHERE schemaname = 'public'
                      AND tablename = 'agent_conversation_context'
                      AND policyname = 'agent_conversation_context_tenant'
                ) THEN
                    CREATE POLICY agent_conversation_context_tenant
                    ON agent_conversation_context
                    USING (tenant_id = current_setting('app.current_tenant_id', TRUE)::uuid);
                END IF;
            END $$;
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "DROP POLICY IF EXISTS agent_conversation_context_tenant ON agent_conversation_context"
        )
        op.execute("ALTER TABLE agent_conversation_context DISABLE ROW LEVEL SECURITY")
        op.execute(
            "DROP POLICY IF EXISTS business_heuristic_overrides_tenant ON business_heuristic_overrides"
        )
        op.execute("ALTER TABLE business_heuristic_overrides DISABLE ROW LEVEL SECURITY")

    op.execute("DROP INDEX IF EXISTS idx_conversation_context_user")
    op.execute("DROP INDEX IF EXISTS idx_conversation_context_tenant")
    op.execute("DROP TABLE IF EXISTS agent_conversation_context")

    op.execute("DROP INDEX IF EXISTS idx_heuristic_overrides_tenant")
    op.execute("DROP TABLE IF EXISTS business_heuristic_overrides")

    op.drop_column("business_profiles", "heuristics_version")
