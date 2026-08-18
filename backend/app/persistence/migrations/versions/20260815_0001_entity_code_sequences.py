"""F-ID: entity_code_sequences — contador atómico de código Véktor

Revision ID: 20260815_0001
Revises: 20260813_0001
Create Date: 2026-08-15

Contexto
--------
Primer paso de F-ID (identidad transversal Producto/Cliente/Proveedor, ver
``docs/plans/ingestion-mapping-overhaul.md`` sección F-ID). Tabla NUEVA y
vacía — no toca ninguna tabla existente, no hace falta ``CONCURRENTLY`` (esa
técnica evita bloquear escrituras sobre una tabla POBLADA con tráfico vivo; acá
no hay ni tabla todavía).

Una fila por ``(tenant_id, entity_type, prefix)``. El valor del código Véktor
se entrega con ``UPDATE ... RETURNING`` (no con esta migración — eso lo hace
``entity_code_service.py`` en runtime): atómico, nunca repite un valor ya
entregado a otro tenant/prefijo concurrente. El índice único es la garantía
dura contra dos filas de secuencia para el mismo ``(tenant, entity_type,
prefix)`` — sin eso, una carrera de ``INSERT`` inicial (primera vez que un
prefijo pide número) podría crear dos filas y desincronizar el contador.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260815_0001"
down_revision = "20260813_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "entity_code_sequences",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entity_type", sa.String(length=20), nullable=False),
        sa.Column("prefix", sa.String(length=10), nullable=False),
        sa.Column("next_value", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "entity_type",
            "prefix",
            name="uq_entity_code_sequences_tenant_type_prefix",
        ),
    )


def downgrade() -> None:
    op.drop_table("entity_code_sequences")
