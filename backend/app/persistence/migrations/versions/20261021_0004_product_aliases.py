"""B·8a: alias de producto (`product_aliases`).

Revision ID: 20261021_0004
Revises: 20261021_0003

Otro nombre con el que el negocio llama a un producto, aprendido al vincular a
mano una venta de «Otros». Único por ``(tenant_id, alias_normalizado)``: un alias
identifica un solo producto. Sin FK al archivo ni a la fila de «Otros»: borrar
el archivo no lo borra (lo aprendió una persona, no el archivo).

Aditiva e idempotente (convención E8c; el lock de ``migrations/env.py``
serializa a la API y al worker). Comprueba PRESENCIA, no forma.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261021_0004"
down_revision = "20261021_0003"
branch_labels = None
depends_on = None

_TABLA = "product_aliases"
_INDICE = "ix_product_aliases_tenant_product"


def _insp() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def upgrade() -> None:
    bind = op.get_bind()
    es_pg = bind.dialect.name == "postgresql"
    uuid_t: sa.types.TypeEngine[object] = (
        postgresql.UUID(as_uuid=True) if es_pg else sa.String(36)
    )

    if not _insp().has_table(_TABLA):
        op.create_table(
            _TABLA,
            sa.Column("id", uuid_t, primary_key=True),
            sa.Column(
                "tenant_id",
                uuid_t,
                sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "product_id",
                uuid_t,
                sa.ForeignKey("products.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("alias", sa.String(300), nullable=False),
            sa.Column("alias_normalizado", sa.String(300), nullable=False),
            sa.Column("origin", sa.String(20), nullable=False, server_default="manual"),
            sa.Column(
                "created_by_user_id",
                uuid_t,
                sa.ForeignKey("users.user_id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint(
                "tenant_id", "alias_normalizado", name="uq_product_aliases_tenant_alias"
            ),
            sa.CheckConstraint("origin IN ('manual')", name="ck_product_aliases_origin"),
        )

    indices = {ix["name"] for ix in _insp().get_indexes(_TABLA)}
    if _INDICE not in indices:
        op.create_index(_INDICE, _TABLA, ["tenant_id", "product_id"])


def downgrade() -> None:
    if _insp().has_table(_TABLA):
        op.drop_table(_TABLA)
