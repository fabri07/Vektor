"""PIN de seguridad por usuario + permiso de modificación de sub-cuentas

Revision ID: 20260723_0001
Revises: 20260722_0002
Create Date: 2026-07-23

Contexto
--------
Migración ADDITIVE — step-up auth con PIN de 4 dígitos. Agrega a ``users``:

  1. ``users.pin_hash`` (Text, nullable) — hash bcrypt del PIN. NULL = sin
     configurar todavía.
  2. ``users.pin_set_at`` (DateTime tz, nullable) — cuándo se estableció.
  3. ``users.can_modify_sensitive`` (Boolean, NOT NULL, default false) — permiso
     que el OWNER otorga a sub-cuentas para editar/borrar datos. El OWNER está
     habilitado implícitamente (no depende de esta columna).

Todo nullable/additive con server_default — no reescribe datos. Usuarios
existentes quedan con ``pin_hash=NULL`` y ``can_modify_sensitive=false``.
``downgrade`` simétrico.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260723_0001"
down_revision = "20260722_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("pin_hash", sa.Text(), nullable=True))
    op.add_column(
        "users", sa.Column("pin_set_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "users",
        sa.Column(
            "can_modify_sensitive",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    # Continuidad: los ADMIN existentes conservaban modify access vía
    # require_role('OWNER','ADMIN'). Backfill para que NO lo pierdan al desplegar
    # (los ADMIN nuevos arrancan en false; el OWNER decide en Configuraciones).
    op.execute("UPDATE users SET can_modify_sensitive = true WHERE role_code = 'ADMIN'")


def downgrade() -> None:
    op.drop_column("users", "can_modify_sensitive")
    op.drop_column("users", "pin_set_at")
    op.drop_column("users", "pin_hash")
