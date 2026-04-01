"""security_fase0_rls_pending_actions_cipher

Revision ID: 20260401_0001
Revises: 0f985a5b0ec0
Create Date: 2026-04-01 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260401_0001"
down_revision: Union[str, None] = "0f985a5b0ec0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── a) Crear tabla pending_actions ────────────────────────────────────────
    op.create_table(
        "pending_actions",
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
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("action_type", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("risk_level", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW() + INTERVAL '10 minutes'"),
        ),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.CheckConstraint(
            "risk_level IN ('MEDIUM','HIGH')",
            name="ck_pending_actions_risk_level",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','APPROVED','REJECTED','EXPIRED')",
            name="ck_pending_actions_status",
        ),
    )
    op.create_index(
        "idx_pending_business_status",
        "pending_actions",
        ["tenant_id", "status"],
    )

    # ── b) approvals no existe en este proyecto — omitido intencionalmente ────

    # ── c) Agregar columnas a health_score_snapshots (IF NOT EXISTS) ──────────
    op.execute(
        "ALTER TABLE health_score_snapshots "
        "ADD COLUMN IF NOT EXISTS cash_score DECIMAL(5,2)"
    )
    op.execute(
        "ALTER TABLE health_score_snapshots "
        "ADD COLUMN IF NOT EXISTS stock_score DECIMAL(5,2)"
    )
    op.execute(
        "ALTER TABLE health_score_snapshots "
        "ADD COLUMN IF NOT EXISTS supplier_score DECIMAL(5,2)"
    )
    op.execute(
        "ALTER TABLE health_score_snapshots "
        "ADD COLUMN IF NOT EXISTS discipline_score DECIMAL(5,2)"
    )

    # ── d) Habilitar RLS en todas las tablas tenant ───────────────────────────
    # Tablas que existen en este proyecto y tienen tenant_id.
    # cash_movements, inventory_*, gmail_messages, events, audit_logs no existen.
    op.execute(
        """
        DO $$ DECLARE tbl TEXT;
        BEGIN
          FOR tbl IN SELECT unnest(ARRAY[
            'sales_entries','expense_entries','products','uploaded_files',
            'health_score_snapshots','decision_audit_log','notifications',
            'user_activity_events','pending_actions'
          ]) LOOP
            EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', tbl);
            IF NOT EXISTS (
              SELECT 1 FROM pg_policies
              WHERE tablename = tbl
                AND policyname = tbl || '_tenant'
            ) THEN
              EXECUTE format(
                'CREATE POLICY %I ON %I '
                'USING (tenant_id = current_setting(''app.current_tenant_id'', TRUE)::uuid)',
                tbl || '_tenant', tbl
              );
            END IF;
          END LOOP;
        END $$;
        """
    )


def downgrade() -> None:
    # Deshabilitar RLS y eliminar políticas
    op.execute(
        """
        DO $$ DECLARE tbl TEXT;
        BEGIN
          FOR tbl IN SELECT unnest(ARRAY[
            'sales_entries','expense_entries','products','uploaded_files',
            'health_score_snapshots','decision_audit_log','notifications',
            'user_activity_events','pending_actions'
          ]) LOOP
            EXECUTE format('ALTER TABLE %I DISABLE ROW LEVEL SECURITY', tbl);
            EXECUTE format('DROP POLICY IF EXISTS %I ON %I',
              tbl || '_tenant', tbl);
          END LOOP;
        END $$;
        """
    )

    # Eliminar columnas de health_score_snapshots
    op.drop_column("health_score_snapshots", "discipline_score")
    op.drop_column("health_score_snapshots", "supplier_score")
    op.drop_column("health_score_snapshots", "stock_score")
    op.drop_column("health_score_snapshots", "cash_score")

    # Eliminar pending_actions
    op.drop_index("idx_pending_business_status", table_name="pending_actions")
    op.drop_table("pending_actions")
