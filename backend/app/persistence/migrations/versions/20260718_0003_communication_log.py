"""communication_log (Comunicación — Fase 5)

Revision ID: 20260718_0003
Revises: 20260718_0002
Create Date: 2026-07-18

Contexto
--------
Migración ADDITIVE y reversible. Crea la tabla `communication_log`: registro
insert-only de mensajes salientes a clientes/proveedores (email funcional vía
Resend; WhatsApp dormido feature-flagged).

  - `tenant_id` FK CASCADE a `tenants.tenant_id` (multi-tenant).
  - `recipient_type` ("customer"|"supplier") + `recipient_id` (UUID, sin FK rígida
    porque el destinatario puede ser de dos tablas distintas).
  - `channel` ("email"|"whatsapp"), `subject` (nullable), `body` (Text).
  - `status` ("sent"|"failed") + `error` (Text nullable).
  - Índice `ix_communication_log_tenant` para el historial por tenant.

No requiere backfill: tabla nueva.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "20260718_0003"
down_revision = "20260718_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "communication_log",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("recipient_type", sa.String(20), nullable=False),
        sa.Column("recipient_id", UUID(as_uuid=True), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("subject", sa.String(300), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_communication_log_tenant", "communication_log", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_communication_log_tenant", table_name="communication_log")
    op.drop_table("communication_log")
