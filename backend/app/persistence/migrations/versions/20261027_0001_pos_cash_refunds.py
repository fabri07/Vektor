"""A-4: ``pos_cash_refunds`` — el reintegro de un ticket de otro turno u otro día.

Revision ID: 20261027_0001
Revises: 20261026_0001

Anular un ticket de caja de AYER, o de un turno que ya se cerró, sacaba la venta
del día de la venta (cuyo arqueo ya estaba firmado) y no dejaba ningún registro
de la plata que hoy salió del cajón: el arqueo del turno abierto y el cierre
diario de hoy daban un faltante sin explicación. Esta tabla guarda de qué turno
y qué día salió ese efectivo, sin tocar el arqueo firmado.

Aditiva e idempotente (convención E8c; el lock de ``migrations/env.py``
serializa a la API y al worker). Comprueba PRESENCIA, no forma.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261027_0001"
down_revision = "20261026_0001"
branch_labels = None
depends_on = None

_TABLA = "pos_cash_refunds"
_INDICE_TURNO = "ix_pos_cash_refunds_tenant_session"
_INDICE_FECHA = "ix_pos_cash_refunds_tenant_fecha"


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
                "operation_id",
                uuid_t,
                sa.ForeignKey("pos_operations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            # El turno abierto de cuyo cajón salió la plata; NULL si el ticket
            # no tenía turno (cuenta sólo para el cierre del día).
            sa.Column(
                "session_id",
                uuid_t,
                sa.ForeignKey("pos_cash_sessions.id", ondelete="CASCADE"),
                nullable=True,
            ),
            sa.Column("origin_client_session_id", sa.String(64), nullable=True),
            sa.Column("sale_date", sa.Date(), nullable=False),
            sa.Column("refund_date", sa.Date(), nullable=False),
            sa.Column("cash_ars", sa.Numeric(14, 2), nullable=False),
            sa.Column(
                "user_id",
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
            # Un ticket devuelve su plata UNA vez: la anulación es idempotente.
            sa.UniqueConstraint("operation_id", name="uq_pos_cash_refunds_operacion"),
            sa.CheckConstraint("cash_ars > 0", name="ck_pos_cash_refunds_importe"),
        )

    indices = {ix["name"] for ix in _insp().get_indexes(_TABLA)}
    if _INDICE_TURNO not in indices:
        op.create_index(_INDICE_TURNO, _TABLA, ["tenant_id", "session_id"])
    if _INDICE_FECHA not in indices:
        op.create_index(_INDICE_FECHA, _TABLA, ["tenant_id", "refund_date"])


def downgrade() -> None:
    if _insp().has_table(_TABLA):
        op.drop_table(_TABLA)
