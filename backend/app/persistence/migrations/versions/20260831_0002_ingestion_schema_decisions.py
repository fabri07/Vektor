"""Bloque 5 — tabla ingestion_schema_decisions (persistencia por huella)

Revision ID: 20260831_0002
Revises: 20260831_0001
Create Date: 2026-08-31

Contexto
--------
Migración ADDITIVE — tabla nueva.

Decisiones EXPLÍCITAS del usuario sobre cómo interpretar un archivo/hoja
(columna→campo, entidad de la hoja, inclusión, tratamiento de stock, decisión
de envío), recuperables cuando el MISMO tenant vuelve a subir un archivo con
la misma huella de esquema (tipo + columnas normalizadas, sin importar orden
ni file_id). Nunca sugerencias automáticas — solo lo que el usuario confirmó.

Una fila por (tenant, schema_fingerprint, context_signature, decision_type):
así una relectura puede actualizar solo `stock_treatment` sin pisar
`column_mapping`. `format_version` permite invalidar el FORMATO del payload
(no el dato) en el futuro sin migración: subir la constante en código hace
que las filas viejas dejen de leerse.

Gateado por `INGESTION_SCHEMA_DECISIONS_ROLLOUT_TENANT_IDS` (lista vacía por
defecto).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260831_0002"
down_revision = "20260831_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ingestion_schema_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("schema_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("context_signature", sa.String(length=64), nullable=False),
        sa.Column("decision_type", sa.String(length=30), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("format_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "tenant_id",
            "schema_fingerprint",
            "context_signature",
            "decision_type",
            name="uq_ingestion_schema_decisions_tenant_schema_context_type",
        ),
        sa.CheckConstraint(
            "decision_type IN ("
            "'column_mapping','context_entity','context_included',"
            "'stock_treatment','shipping_decision')",
            name="ck_ingestion_schema_decisions_type",
        ),
    )
    op.create_index(
        "ix_ingestion_schema_decisions_lookup",
        "ingestion_schema_decisions",
        ["tenant_id", "schema_fingerprint", "context_signature"],
    )


def downgrade() -> None:
    op.drop_index("ix_ingestion_schema_decisions_lookup", table_name="ingestion_schema_decisions")
    op.drop_table("ingestion_schema_decisions")
