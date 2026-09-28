"""Turno de caja y su arqueo (B8 del POS).

Revision ID: 20260929_0001
Revises: 20260927_0002

Un turno es de UN cajero en UNA caja: abre con su fondo inicial, cobra, y al
cerrar cuenta el efectivo del cajón contra lo que el sistema esperaba.

**El id del turno lo genera la caja, no el servidor** (``client_session_id``,
que B7 ya venía guardando en cada ``pos_operation``). Si abrir turno exigiera
una llamada, la caja no podría abrirlo sin internet, que es justo lo que B7b
habilitó. El servidor **adopta** ese id: de ahí el único por tenant.

**Un arqueo cerrado no se reescribe.** ``expected_cash_ars`` y
``counted_cash_ars`` se congelan al cerrar; una venta que sincroniza después no
los toca y queda en la conciliación posterior (``pos_late_sales``). Si se
reescribieran, el número que el cajero firmó dejaría de ser el que firmó.

Aditiva e idempotente (convención E8c; el lock de ``migrations/env.py``
serializa a la API y al worker). Comprueba PRESENCIA, no forma.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260929_0001"
down_revision = "20260927_0002"
branch_labels = None
depends_on = None

_TABLA = "pos_cash_sessions"
_TARDIAS = "pos_late_sales"
_INDICE_ESTADO = "ix_pos_cash_sessions_tenant_status"
_UNICO_CLIENTE = "uq_pos_cash_sessions_client_session"


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
            # El id que generó la caja al abrir el turno. Es lo que liga las
            # ventas (`pos_operations.client_session_id`) con este registro, y
            # lo que permite abrir turno sin red.
            sa.Column("client_session_id", sa.String(64), nullable=False),
            # SET NULL y no CASCADE: dar de baja una caja o un empleado no puede
            # borrar el arqueo de un turno que ya se cerró.
            sa.Column(
                "terminal_id",
                uuid_t,
                sa.ForeignKey("pos_terminals.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "user_id",
                uuid_t,
                sa.ForeignKey("users.user_id", ondelete="SET NULL"),
                nullable=True,
            ),
            # Lo que había en el cajón al abrir. Lo declara quien abre.
            sa.Column(
                "opening_float_ars",
                sa.Numeric(14, 2),
                nullable=False,
                server_default="0",
            ),
            sa.Column(
                "opened_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "closed_by_user_id",
                uuid_t,
                sa.ForeignKey("users.user_id", ondelete="SET NULL"),
                nullable=True,
            ),
            # Los tres se escriben AL CERRAR y no se vuelven a tocar.
            sa.Column("expected_cash_ars", sa.Numeric(14, 2), nullable=True),
            sa.Column("counted_cash_ars", sa.Numeric(14, 2), nullable=True),
            sa.Column("difference_ars", sa.Numeric(14, 2), nullable=True),
            sa.Column("notes", sa.String(500), nullable=True),
            sa.Column(
                "status", sa.String(20), nullable=False, server_default="OPEN"
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
            sa.CheckConstraint(
                "status IN ('OPEN','CLOSED')", name="ck_pos_cash_sessions_status"
            ),
            sa.CheckConstraint(
                "opening_float_ars >= 0", name="ck_pos_cash_sessions_fondo"
            ),
        )
        op.create_index("ix_pos_cash_sessions_tenant_id", _TABLA, ["tenant_id"])

    indices = {ix["name"] for ix in _insp().get_indexes(_TABLA)}
    únicos = {u["name"] for u in _insp().get_unique_constraints(_TABLA)}
    if _INDICE_ESTADO not in indices:
        op.create_index(_INDICE_ESTADO, _TABLA, ["tenant_id", "status"])
    # El candado que hace que dos aperturas simultáneas del mismo turno —la
    # explícita y la que adopta la primera venta— no creen dos filas.
    if _UNICO_CLIENTE not in indices | únicos:
        op.create_index(
            _UNICO_CLIENTE, _TABLA, ["tenant_id", "client_session_id"], unique=True
        )

    if not _insp().has_table(_TARDIAS):
        # Una venta que llegó después de que su turno cerrara. No modifica el
        # arqueo firmado: queda acá para que alguien la concilie.
        op.create_table(
            _TARDIAS,
            sa.Column("id", uuid_t, primary_key=True),
            sa.Column(
                "tenant_id",
                uuid_t,
                sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "session_id",
                uuid_t,
                sa.ForeignKey("pos_cash_sessions.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "operation_id",
                uuid_t,
                sa.ForeignKey("pos_operations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            # Cuánto de esa venta era efectivo: es lo único que habría cambiado
            # el arqueo, y guardarlo evita recalcularlo sobre datos que pueden
            # haber cambiado (una anulación posterior, por ejemplo).
            sa.Column("cash_ars", sa.Numeric(14, 2), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint("operation_id", name="uq_pos_late_sales_operacion"),
        )
        op.create_index(
            "ix_pos_late_sales_tenant_session", _TARDIAS, ["tenant_id", "session_id"]
        )


def downgrade() -> None:
    # Las tardías primero: tienen FK a las sesiones.
    if _insp().has_table(_TARDIAS):
        op.drop_table(_TARDIAS)
    if _insp().has_table(_TABLA):
        op.drop_table(_TABLA)
