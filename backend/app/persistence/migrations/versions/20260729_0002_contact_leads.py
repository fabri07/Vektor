"""Tabla contact_leads — leads del formulario público de contacto.

Revision ID: 20260729_0002
Revises: 20260729_0001
Create Date: 2026-07-16

Contexto
--------
El formulario público de "Contactanos" (landing) persiste el lead en Postgres y
notifica al equipo por email de forma DESACOPLADA. La tabla no tiene tenant_id:
la genera un visitante anónimo. Guarda datos de contacto, estado operativo del
lead, estado de la notificación por email, consentimiento de privacidad
(Ley 25.326) con su versión y fecha, y un HASH de IP (no la IP cruda) para
prevención de abuso/spam.

Additive puro. Idempotente vía ``sa.inspect`` (varios servicios de Railway pueden
correr ``alembic upgrade head`` en paralelo en un mismo deploy).
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "20260729_0002"
down_revision: str | None = "20260729_0001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "contact_leads" in inspector.get_table_names():
        return

    op.create_table(
        "contact_leads",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("nombre", sa.String(length=120), nullable=False),
        sa.Column("celular", sa.String(length=40), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("empresa", sa.String(length=160), nullable=False),
        sa.Column("rubro", sa.String(length=40), nullable=False),
        sa.Column("cantidad_usuarios", sa.String(length=20), nullable=False),
        sa.Column("gestion_actual", sa.Text(), nullable=True),
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default="new"
        ),
        sa.Column(
            "email_notification_status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("consent_version", sa.String(length=20), nullable=False),
        sa.Column(
            "consent_accepted_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column("cta_source", sa.String(length=60), nullable=True),
        sa.Column("ip_hash", sa.String(length=64), nullable=True),
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
    )
    op.create_index("ix_contact_leads_email", "contact_leads", ["email"])
    op.create_index("ix_contact_leads_status", "contact_leads", ["status"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "contact_leads" not in inspector.get_table_names():
        return
    op.drop_index("ix_contact_leads_status", table_name="contact_leads")
    op.drop_index("ix_contact_leads_email", table_name="contact_leads")
    op.drop_table("contact_leads")
