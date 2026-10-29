"""F5 (panel SUPERADMIN): ``tenant_daily_activity`` — actividad por usuario y día.

Revision ID: 20261029_0002
Revises: 20261029_0001

Una fila por (usuario, día de negocio AR). La clave primaria es la unicidad: el
registro es ``INSERT ... ON CONFLICT DO NOTHING``, idempotente y sin Redis.

Aditiva e idempotente (convención E8c; el lock de ``migrations/env.py`` serializa
a la API y al worker). Comprueba PRESENCIA de la tabla, no su forma.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261029_0002"
down_revision = "20261029_0001"
branch_labels = None
depends_on = None

_TABLA = "tenant_daily_activity"
_INDICE = "ix_tenant_daily_activity_tenant_id"


def _insp() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def upgrade() -> None:
    if _insp().has_table(_TABLA):
        return
    op.create_table(
        _TABLA,
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.user_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("activity_date", sa.Date(), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(_INDICE, _TABLA, ["tenant_id"])


def downgrade() -> None:
    if _insp().has_table(_TABLA):
        op.drop_table(_TABLA)
