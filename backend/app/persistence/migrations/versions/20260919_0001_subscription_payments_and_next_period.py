"""Pagos de suscripción + período siguiente + día ancla (bloque D).

Revision ID: 20260919_0001
Revises: 20260918_0001

Aditiva y sin backfill:

* ``subscription_payments`` — un pago acreditado y el período que otorgó. El
  UNIQUE ``(source, reference)`` es el candado de la idempotencia: dos
  ejecuciones simultáneas de la misma referencia no otorgan dos períodos.
* ``subscriptions.next_*`` — período siguiente ya pago (pago anticipado) o
  cambio de plan programado. Todas nullable: una suscripción existente no
  tiene período siguiente, que es exactamente lo que NULL significa.
* ``subscriptions.billing_anchor_day`` — nullable a propósito. NO se rellena
  desde ``current_period_start``: el ancla es una condición comercial que se
  fija al cobrar; inventarla para filas viejas sería declarar un ciclo que
  nadie acordó. El servicio la fija en la primera activación/renovación.

Idempotente (convención E8c): el ``preDeployCommand`` de Railway corre
``alembic upgrade head`` en cada deploy, y un esquema por delante de
``alembic_version`` no puede tumbarlo. Comprueba PRESENCIA, no forma.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260919_0001"
down_revision = "20260918_0001"
branch_labels = None
depends_on = None

_TABLA = "subscription_payments"

_COLUMNAS_NUEVAS: tuple[tuple[str, sa.types.TypeEngine[object]], ...] = (
    ("next_period_end", sa.DateTime(timezone=True)),
    ("next_plan_code", sa.Text()),
    ("next_seats_included", sa.Integer()),
    ("next_granted_ia_queries_per_month", sa.Integer()),
    ("next_granted_imports_per_month", sa.Integer()),
    ("next_granted_photo_pdf_reads_per_month", sa.Integer()),
    ("billing_anchor_day", sa.Integer()),
)


def _tabla_existe(bind: sa.Connection, tabla: str) -> bool:
    return sa.inspect(bind).has_table(tabla)


def _columnas(bind: sa.Connection, tabla: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(tabla)}


def upgrade() -> None:
    bind = op.get_bind()
    es_pg = bind.dialect.name == "postgresql"

    existentes = _columnas(bind, "subscriptions")
    for nombre, tipo in _COLUMNAS_NUEVAS:
        if nombre not in existentes:
            op.add_column("subscriptions", sa.Column(nombre, tipo, nullable=True))

    if not _tabla_existe(bind, _TABLA):
        uuid_t = postgresql.UUID(as_uuid=True) if es_pg else sa.String(36)
        json_t = postgresql.JSONB() if es_pg else sa.JSON()
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
                "subscription_id",
                uuid_t,
                sa.ForeignKey("subscriptions.subscription_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("source", sa.String(30), nullable=False),
            sa.Column("reference", sa.String(200), nullable=False),
            sa.Column("kind", sa.String(30), nullable=False),
            sa.Column("plan_code", sa.Text(), nullable=False),
            sa.Column("amount_usd", sa.Numeric(8, 2), nullable=False),
            sa.Column("amount_ars", sa.Numeric(14, 2), nullable=False),
            sa.Column("operator", sa.String(100), nullable=False),
            sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
            sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
            sa.Column("granted_json", json_t, nullable=False),
            sa.Column("notes", sa.Text(), nullable=True),
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
            sa.UniqueConstraint(
                "source", "reference", name="uq_subscription_payments_source_reference"
            ),
        )
        op.create_index(
            "ix_subscription_payments_tenant", _TABLA, ["tenant_id", "period_start"]
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _tabla_existe(bind, _TABLA):
        op.drop_table(_TABLA)
    existentes = _columnas(bind, "subscriptions")
    presentes = [n for n, _ in _COLUMNAS_NUEVAS if n in existentes]
    if not presentes:
        return
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("subscriptions") as batch:
            for nombre in presentes:
                batch.drop_column(nombre)
    else:
        for nombre in presentes:
            op.drop_column("subscriptions", nombre)
