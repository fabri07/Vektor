"""Tabla tenant_maintenance_locks (Fase 3 — dedup auditado de productos)

Revision ID: 20260731_0004
Revises: 20260731_0003
Create Date: 2026-07-31

Contexto
--------
Migración ADDITIVE — crea ``tenant_maintenance_locks``, el lease por-tenant
que va a usar el script de dedup (Fase 3) para evitar correr un merge de
productos en paralelo con otra corrida del mismo tenant (o con imports en
vuelo). Una fila por tenant activo (``UniqueConstraint`` sobre ``tenant_id``):
adquirir el lock es un upsert/insert que falla si ya hay uno vigente; el
caller es responsable de expirar/liberar (``expires_at``/``heartbeat_at``).
No reescribe ni depende de datos existentes.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260731_0004"
down_revision = "20260731_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenant_maintenance_locks",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("lease_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reason", sa.String(200), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(120), nullable=False),
        sa.UniqueConstraint("tenant_id", name="uq_tenant_maintenance_locks_tenant"),
    )


def downgrade() -> None:
    op.drop_table("tenant_maintenance_locks")
