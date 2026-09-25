"""Terminales de caja habilitadas (B6 del POS).

Revision ID: 20260926_0001
Revises: 20260925_0001

* ``pos_terminals``: una PC habilitada por el dueño para cobrar. Guarda el
  sha256 del secreto, nunca el secreto. Dar de baja es ``disabled_at``.
* ``pos_operations.terminal_id``: desde qué caja se cobró. Nullable y sin
  backfill: lo histórico y lo que el dueño carga desde el navegador no tienen
  terminal, y rellenarlo sería inventar.

Aditiva e idempotente (convención E8c; el lock de ``migrations/env.py``
serializa a la API y al worker). Comprueba PRESENCIA, no forma.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260926_0001"
down_revision = "20260925_0001"
branch_labels = None
depends_on = None

_TABLA = "pos_terminals"
_INDICE_NOMBRE = "uq_pos_terminals_tenant_name_activa"
_FK_OPERACION = "fk_pos_operations_terminal_id"


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
            sa.Column("name", sa.String(80), nullable=False),
            sa.Column("credential_hash", sa.String(64), nullable=False),
            sa.Column(
                "created_by_user_id",
                uuid_t,
                sa.ForeignKey("users.user_id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "disabled_by_user_id",
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
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint("credential_hash", name="uq_pos_terminals_credential_hash"),
        )
        op.create_index("ix_pos_terminals_tenant_id", _TABLA, ["tenant_id"])

    indices = {ix["name"] for ix in _insp().get_indexes(_TABLA)}
    if _INDICE_NOMBRE not in indices:
        op.create_index(
            _INDICE_NOMBRE,
            _TABLA,
            ["tenant_id", "name"],
            unique=True,
            postgresql_where=sa.text("disabled_at IS NULL"),
            sqlite_where=sa.text("disabled_at IS NULL"),
        )

    columnas = {c["name"] for c in _insp().get_columns("pos_operations")}
    if "terminal_id" not in columnas:
        if es_pg:
            op.add_column("pos_operations", sa.Column("terminal_id", uuid_t, nullable=True))
            op.create_foreign_key(
                _FK_OPERACION,
                "pos_operations",
                _TABLA,
                ["terminal_id"],
                ["id"],
                ondelete="SET NULL",
            )
        else:
            with op.batch_alter_table("pos_operations") as batch:
                batch.add_column(sa.Column("terminal_id", uuid_t, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    columnas = {c["name"] for c in _insp().get_columns("pos_operations")}
    if "terminal_id" in columnas:
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table("pos_operations") as batch:
                batch.drop_column("terminal_id")
        else:
            fks = {fk["name"] for fk in _insp().get_foreign_keys("pos_operations")}
            if _FK_OPERACION in fks:
                op.drop_constraint(_FK_OPERACION, "pos_operations", type_="foreignkey")
            op.drop_column("pos_operations", "terminal_id")
    if _insp().has_table(_TABLA):
        op.drop_table(_TABLA)
