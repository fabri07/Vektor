"""Sprint 21: tabla tenant_column_mappings (mapeo aprendido de columnas por tenant)

Revision ID: 20260620_0001
Revises: 20260617_0001
Create Date: 2026-06-20

Contexto
--------
Migración ADDITIVE: nueva tabla `tenant_column_mappings` para almacenar el mapeo
aprendido de columnas por tenant. Registra qué columna de un archivo subido
corresponde a qué campo canónico del dominio, acumulando confirmaciones del usuario
para aumentar la confianza de futuras sugerencias automáticas.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260620_0001"
down_revision = "20260617_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenant_column_mappings",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entity_type", sa.String(30), nullable=False),
        sa.Column("source_column", sa.String(200), nullable=False),
        sa.Column("target_field", sa.String(80), nullable=False),
        sa.Column(
            "confirmed_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "entity_type",
            "source_column",
            name="uq_tcm_tenant_entity_col",
        ),
        sa.CheckConstraint(
            "entity_type IN ('sale', 'expense', 'product', 'inventory')",
            name="ck_tcm_entity_type",
        ),
    )
    op.create_index(
        "ix_tcm_tenant_entity",
        "tenant_column_mappings",
        ["tenant_id", "entity_type"],
    )


def downgrade() -> None:
    op.drop_index("ix_tcm_tenant_entity", table_name="tenant_column_mappings")
    op.drop_table("tenant_column_mappings")
