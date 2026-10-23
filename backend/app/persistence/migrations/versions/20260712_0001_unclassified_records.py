"""unclassified_records — bandeja de revisión "Otros"

Revision ID: 20260712_0001
Revises: 20260710_0001
Create Date: 2026-07-12

Contexto
--------
Migración ADDITIVE. Tabla nueva para los datos que llegan por chat o ingesta y
NO se clasifican como venta/gasto/producto: hojas no clasificables, filas
ambiguas (antes caían al bucket de ventas por default), filas con monto/fecha
no parseables (antes se salteaban en silencio), y hallazgos del reanálisis
retroactivo. El tenant las revisa en /otros: importa (sale/expense/product)
o descarta.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260712_0001"
down_revision = "20260710_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "unclassified_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "uploaded_file_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("uploaded_files.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("context_label", sa.String(200), nullable=True),
        sa.Column("headers", postgresql.JSONB(), nullable=True),
        sa.Column("row_data", postgresql.JSONB(), nullable=False),
        sa.Column("suggested_entity", sa.String(10), nullable=True),
        sa.Column("status", sa.String(15), nullable=False, server_default="PENDING"),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
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
            "status IN ('PENDING', 'IMPORTED', 'DISMISSED')",
            name="ck_unclassified_records_status",
        ),
        sa.CheckConstraint(
            "source IN ('ingestion', 'chat', 'reanalysis')",
            name="ck_unclassified_records_source",
        ),
    )
    op.create_index(
        "ix_unclassified_records_tenant_id", "unclassified_records", ["tenant_id"]
    )
    op.create_index(
        "ix_unclassified_records_tenant_status",
        "unclassified_records",
        ["tenant_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_unclassified_records_tenant_status", table_name="unclassified_records")
    op.drop_index("ix_unclassified_records_tenant_id", table_name="unclassified_records")
    op.drop_table("unclassified_records")
