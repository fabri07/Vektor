"""Capital de los socios: tabla ``owner_capital_movements`` (D·2).

Revision ID: 20261009_0001
Revises: 20261002_0001

Un aporte, un retiro o un préstamo de socio no es venta ni gasto: tiene su
propia tabla para no contaminar ventas, margen, gastos ni el score. El importe
es siempre positivo (CHECK) y el sentido lo da ``kind``. No se borra: se anula
con ``voided_at``/``voided_by_user_id``/``void_reason``.

Aditiva e idempotente (convención E8c; el lock de ``migrations/env.py``
serializa a la API y al worker). Comprueba PRESENCIA, no forma.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261009_0001"
down_revision = "20261002_0001"
branch_labels = None
depends_on = None

_TABLA = "owner_capital_movements"
_INDICE_FECHA = "ix_owner_capital_movements_tenant_date"
_INDICE_TENANT = "ix_owner_capital_movements_tenant_id"

_METODOS = "('cash','debit_card','credit_card','transfer','qr','account','other')"


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
            sa.Column("kind", sa.String(30), nullable=False),
            sa.Column("form", sa.String(20), nullable=False, server_default="dinero"),
            sa.Column("partner_name", sa.String(120), nullable=True),
            sa.Column("occurred_on", sa.Date(), nullable=False),
            sa.Column("payment_method", sa.String(30), nullable=True),
            sa.Column("amount_ars", sa.Numeric(14, 2), nullable=False),
            sa.Column("notes", sa.String(1000), nullable=True),
            sa.Column(
                "source_type", sa.String(30), nullable=False, server_default="manual"
            ),
            sa.Column(
                "source_upload_id",
                uuid_t,
                sa.ForeignKey("uploaded_files.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("source_ref", sa.String(100), nullable=True),
            # SET NULL: dar de baja a un usuario no borra el capital que cargó.
            sa.Column(
                "created_by_user_id",
                uuid_t,
                sa.ForeignKey("users.user_id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "voided_by_user_id",
                uuid_t,
                sa.ForeignKey("users.user_id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("void_reason", sa.String(500), nullable=True),
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
                "kind IN ('aporte','retiro','prestamo_socio','devolucion_prestamo')",
                name="ck_owner_capital_movements_kind",
            ),
            sa.CheckConstraint(
                "form IN ('dinero','especie')", name="ck_owner_capital_movements_form"
            ),
            sa.CheckConstraint(
                "source_type IN ('manual','archivo','onboarding_declarado','migrado_de_gasto')",
                name="ck_owner_capital_movements_source_type",
            ),
            sa.CheckConstraint(
                "amount_ars > 0", name="ck_owner_capital_movements_amount_positive"
            ),
            sa.CheckConstraint(
                # IS NOT NULL: con NULL el IN da NULL, y un CHECK NULL pasa.
                "(form = 'dinero' AND payment_method IS NOT NULL"
                f" AND payment_method IN {_METODOS})"
                " OR (form = 'especie' AND payment_method IS NULL)",
                name="ck_owner_capital_movements_payment_method",
            ),
        )

    indices = {ix["name"] for ix in _insp().get_indexes(_TABLA)}
    if _INDICE_TENANT not in indices:
        op.create_index(_INDICE_TENANT, _TABLA, ["tenant_id"])
    if _INDICE_FECHA not in indices:
        op.create_index(_INDICE_FECHA, _TABLA, ["tenant_id", "occurred_on"])


def downgrade() -> None:
    if _insp().has_table(_TABLA):
        op.drop_table(_TABLA)
