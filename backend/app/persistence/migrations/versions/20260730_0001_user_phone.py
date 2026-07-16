"""Teléfono/WhatsApp de contacto del usuario

Revision ID: 20260730_0001
Revises: 20260729_0002
Create Date: 2026-07-30

Contexto
--------
Migración ADDITIVE — agrega ``users.phone`` (String(50), nullable): el
teléfono/WhatsApp de contacto del usuario, pedido opcionalmente en el registro
y editable en /settings. Informativo (los links wa.me hacia clientes/proveedores
no lo necesitan). Usuarios existentes quedan con ``phone=NULL``.
``downgrade`` simétrico.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260730_0001"
down_revision = "20260729_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("phone", sa.String(length=50), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "phone")
