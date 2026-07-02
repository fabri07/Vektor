"""Tabla chat_coverage_gaps — backlog de producto desde rechazos del chat.

Revision ID: 20260726_0001
Revises: 20260725_0001
Create Date: 2026-07-26

Contexto
--------
Migración ADDITIVE. Hoy, cuando el chat rechaza una consulta (out_of_scope,
intent_desconocido, gate de baja confianza), el rechazo se loguea como
clasificación exitosa y es invisible como señal de producto. Esta tabla los
captura (insert-only, best-effort desde CoverageGapService) para revisarlos
como backlog: qué preguntan los usuarios que Véktor todavía no cubre.

RLS: misma política tenant que el resto de las tablas de negocio
(patrón de 20260401_0001).

``downgrade`` dropea la tabla (log operativo, reversible sin pérdida de
datos de negocio).
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260726_0001"
down_revision = "20260725_0001"
branch_labels = None
depends_on = None

_TABLE = "chat_coverage_gaps"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("original_message", sa.Text(), nullable=False),
        sa.Column("classified_intent", sa.String(60), nullable=True),
        sa.Column("classified_domain", sa.String(40), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("fallback_reason", sa.String(30), nullable=False),
        sa.Column("ui_context", postgresql.JSONB(), nullable=True),
        sa.Column(
            "reviewed", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "fallback_reason IN ("
            "'out_of_scope','intent_desconocido','baja_confianza',"
            "'sin_datos','ui_context_missing','advice_blocked')",
            name="ck_chat_coverage_gaps_reason",
        ),
    )
    op.create_index(f"ix_{_TABLE}_tenant_id", _TABLE, ["tenant_id"])
    op.create_index(
        f"ix_{_TABLE}_tenant_reviewed", _TABLE, ["tenant_id", "reviewed"]
    )

    # RLS — misma política tenant que el resto (patrón 20260401_0001).
    op.execute(
        f"""
        DO $$
        BEGIN
          EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', '{_TABLE}');
          IF NOT EXISTS (
            SELECT 1 FROM pg_policies
            WHERE tablename = '{_TABLE}'
              AND policyname = '{_TABLE}_tenant'
          ) THEN
            EXECUTE format(
              'CREATE POLICY %I ON %I '
              'USING (tenant_id = current_setting(''app.current_tenant_id'', TRUE)::uuid)',
              '{_TABLE}_tenant', '{_TABLE}'
            );
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
          EXECUTE format('ALTER TABLE %I DISABLE ROW LEVEL SECURITY', '{_TABLE}');
          EXECUTE format('DROP POLICY IF EXISTS %I ON %I', '{_TABLE}_tenant', '{_TABLE}');
        END $$;
        """
    )
    op.drop_index(f"ix_{_TABLE}_tenant_reviewed", table_name=_TABLE)
    op.drop_index(f"ix_{_TABLE}_tenant_id", table_name=_TABLE)
    op.drop_table(_TABLE)
