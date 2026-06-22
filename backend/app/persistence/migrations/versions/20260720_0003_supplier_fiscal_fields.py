"""Campos fiscales de proveedor (persona/empresa) + sentinela único por tenant

Revision ID: 20260720_0003
Revises: 20260720_0002
Create Date: 2026-07-20

Contexto
--------
Migración ADDITIVE — reforma de la sección Proveedores (Fase 2). Un proveedor
puede ser PERSONA (nombre + apellido) o EMPRESA (razón social en ``name``):

  1. ``suppliers.last_name`` (String(200), nullable) — apellido; vacío si es
     empresa/razón social. No se fuerza.
  2. ``suppliers.cuil`` (String(13), nullable) — CUIL/CUIT; validado por formato
     en el schema (no se rellena).
  3. ``suppliers.payment_method`` (String(30), nullable) — forma de pago habitual.

Además crea el ÍNDICE ÚNICO PARCIAL del sentinela "No identificado" (Fase 1):
un único proveedor por tenant con ``custom_fields->>'_sentinel' = 'true'``,
garantizando el invariante a nivel DB (concurrencia de imports).

Todo nullable/additive — no reescribe datos. ``downgrade`` simétrico.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260720_0003"
down_revision = "20260720_0002"
branch_labels = None
depends_on = None

_SENTINEL_INDEX = "uq_suppliers_sentinel_per_tenant"


def upgrade() -> None:
    op.add_column("suppliers", sa.Column("last_name", sa.String(length=200), nullable=True))
    op.add_column("suppliers", sa.Column("cuil", sa.String(length=13), nullable=True))
    op.add_column(
        "suppliers", sa.Column("payment_method", sa.String(length=30), nullable=True)
    )
    # Índice único parcial: UN solo sentinela activo por tenant.
    op.execute(
        f"CREATE UNIQUE INDEX {_SENTINEL_INDEX} ON suppliers (tenant_id) "
        "WHERE (custom_fields->>'_sentinel') = 'true'"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_SENTINEL_INDEX}")
    op.drop_column("suppliers", "payment_method")
    op.drop_column("suppliers", "cuil")
    op.drop_column("suppliers", "last_name")
