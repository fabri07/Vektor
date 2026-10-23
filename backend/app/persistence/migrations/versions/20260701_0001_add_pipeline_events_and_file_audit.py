"""FASE 0: pipeline_events (traza de ingestión) + auditoría en uploaded_files

Revision ID: 20260701_0001
Revises: 20260625_0001
Create Date: 2026-07-01

Contexto
--------
Migración ADDITIVE para observabilidad y preservación del pipeline de ingestión:
- Tabla `pipeline_events`: traza append-only por (trace_id, stage). Registra
  rows_in/out/rejected, latencia, confidence y detalle de rechazos. No persiste
  filas aceptadas (se reconstruyen del crudo en R2 + los registros insertados).
- Columnas en `uploaded_files`: `trace_id` (agrupa eventos), `content_hash`
  (dedup de re-upload) y `deleted_at` (soft delete — el crudo en R2 se preserva).

Todo nullable / additive: no rompe filas existentes ni requiere backfill.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260701_0001"
down_revision = "20260625_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pipeline_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("trace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("file_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("stage", sa.String(20), nullable=False),
        sa.Column("rows_in", sa.Integer(), nullable=True),
        sa.Column("rows_out", sa.Integer(), nullable=True),
        sa.Column("rows_rejected", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("confidence", sa.String(10), nullable=True),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pipeline_events_trace", "pipeline_events", ["trace_id"])
    op.create_index(
        "ix_pipeline_events_tenant_created", "pipeline_events", ["tenant_id", "created_at"]
    )
    op.create_index(
        op.f("ix_pipeline_events_tenant_id"), "pipeline_events", ["tenant_id"]
    )

    op.add_column(
        "uploaded_files",
        sa.Column("trace_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "uploaded_files",
        sa.Column("content_hash", sa.String(64), nullable=True),
    )
    op.add_column(
        "uploaded_files",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("uploaded_files", "deleted_at")
    op.drop_column("uploaded_files", "content_hash")
    op.drop_column("uploaded_files", "trace_id")
    op.drop_index(op.f("ix_pipeline_events_tenant_id"), table_name="pipeline_events")
    op.drop_index("ix_pipeline_events_tenant_created", table_name="pipeline_events")
    op.drop_index("ix_pipeline_events_trace", table_name="pipeline_events")
    op.drop_table("pipeline_events")
