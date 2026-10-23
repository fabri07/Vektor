"""Historial materializado del costo promedio ponderado (M·P2a).

Revision ID: 20261013_0001
Revises: 20261011_0001

``product_cost_history``: un registro por compra, apertura o ancla con el estado
del promedio después (``domain/costo_promedio.py``). ``product_cost_state``: una
fila por producto con la revisión publicada y el día mínimo pendiente. Las dos
son DERIVADAS del ledger: borrarlas no pierde nada, se reconstruyen. El promedio
es un dato; el costo principal de cada venta sigue siendo el de reposición.

Aditiva e idempotente (convención E8c; el lock de ``migrations/env.py``
serializa a la API y al worker). Comprueba PRESENCIA, no forma.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261013_0001"
down_revision = "20261011_0001"
branch_labels = None
depends_on = None

_HISTORIAL = "product_cost_history"
_ESTADO = "product_cost_state"
_INDICE = "ix_product_cost_history_lookup"


def _insp() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def upgrade() -> None:
    bind = op.get_bind()
    uuid_t: sa.types.TypeEngine[object] = (
        postgresql.UUID(as_uuid=True) if bind.dialect.name == "postgresql" else sa.String(36)
    )

    def _fk_tenant() -> sa.Column[object]:
        return sa.Column(
            "tenant_id",
            uuid_t,
            sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
            nullable=False,
        )

    def _fk_producto() -> sa.Column[object]:
        return sa.Column(
            "product_id", uuid_t, sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False
        )

    if not _insp().has_table(_HISTORIAL):
        op.create_table(
            _HISTORIAL,
            sa.Column("id", uuid_t, primary_key=True),
            _fk_tenant(),
            _fk_producto(),
            sa.Column("dia", sa.Date(), nullable=False),
            sa.Column("clase_orden", sa.SmallInteger(), nullable=False),
            sa.Column("fecha", sa.DateTime(timezone=False), nullable=False),
            sa.Column("creado", sa.DateTime(timezone=False), nullable=False),
            sa.Column("evento_id", uuid_t, nullable=False),
            sa.Column("clase", sa.String(10), nullable=False),
            sa.Column("u_conocidas", sa.Numeric(18, 6), nullable=False),
            sa.Column("u_sin_costo", sa.Numeric(18, 6), nullable=False),
            sa.Column("costo_promedio", sa.Numeric(18, 6), nullable=True),
            sa.Column("estado", sa.String(15), nullable=False),
            sa.Column("stock_conocido", sa.Boolean(), nullable=False),
            sa.Column("aproximado", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("motivo", sa.String(200), nullable=True),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.CheckConstraint(
                "estado IN ('medido','incompleto','sin_medicion')",
                name="ck_product_cost_history_estado",
            ),
            sa.CheckConstraint(
                "clase IN ('apertura','compra','ancla','entrada','salida','complejo')",
                name="ck_product_cost_history_clase",
            ),
        )
    indices = {ix["name"] for ix in _insp().get_indexes(_HISTORIAL)}
    if _INDICE not in indices:
        op.create_index(_INDICE, _HISTORIAL, ["tenant_id", "product_id", "dia"])

    if not _insp().has_table(_ESTADO):
        op.create_table(
            _ESTADO,
            sa.Column("id", uuid_t, primary_key=True),
            _fk_tenant(),
            _fk_producto(),
            sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("pendiente_desde", sa.Date(), nullable=True),
            sa.Column(
                "stock_fuera_del_ledger", sa.Boolean(), nullable=False, server_default=sa.false()
            ),
            sa.Column("aproximado", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("reconstruido_en", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("tenant_id", "product_id", name="uq_product_cost_state_product"),
        )


def downgrade() -> None:
    for tabla in (_ESTADO, _HISTORIAL):
        if _insp().has_table(tabla):
            op.drop_table(tabla)
