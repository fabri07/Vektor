"""Permisos de caja por usuario (B5 del POS).

Revision ID: 20260925_0001
Revises: 20260921_0002

``users.pos_permissions JSONB NOT NULL DEFAULT '{}'``: qué puede hacer un
cajero (rol ``CASHIER``) adentro de la caja — descontar, anular un ticket,
fiar, vincular un código de barras. El set de claves es cerrado y vive en
``domain/pos_permissions.py``; sólo un booleano ``true`` habilita. OWNER y
ADMIN tienen todo por rol, así que el ``{}`` de todas las filas existentes no
le cambia nada a nadie.

Aditiva, sin backfill e idempotente (convención E8c): el ``preDeployCommand``
corre en cada deploy, y además lo corren la API y el worker a la vez — el lock
de ``migrations/env.py`` serializa, y este guard cubre un esquema que quedó por
delante de ``alembic_version``. Comprueba PRESENCIA, no forma.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260925_0001"
down_revision = "20260921_0002"
branch_labels = None
depends_on = None

_JSONB = postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")


def _columnas() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns("users")}


def upgrade() -> None:
    if "pos_permissions" in _columnas():
        return
    es_pg = op.get_bind().dialect.name == "postgresql"
    op.add_column(
        "users",
        sa.Column(
            "pos_permissions",
            _JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb") if es_pg else sa.text("'{}'"),
        ),
    )


def downgrade() -> None:
    if "pos_permissions" not in _columnas():
        return
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("users") as batch:
            batch.drop_column("pos_permissions")
    else:
        op.drop_column("users", "pos_permissions")
