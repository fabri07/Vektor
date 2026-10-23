"""Catálogo de planes + estados reales de Subscription + cupos con reserva.

Revision ID: 20260918_0001
Revises: 20260917_0001
Create Date: 2026-09-18

Contexto
--------
Backend de la Etapa 2 de la política de planes (``docs/plans/``): catálogo
versionado, estados de suscripción, prueba y cupos aplicados desde el
backend — todavía sin Mercado Pago.

Tablas nuevas
-------------
* ``plan_definitions`` — catálogo VIGENTE (para nuevas activaciones). Se
  siembra con 4 filas: ``FREE`` (legado, preserva el comportamiento anterior
  — "sin límite mensual configurado", solo queda el rate-limit diario global
  ya existente) y ``esencial``/``control``/``direccion`` con los cupos de la
  política. Editar esta tabla más adelante NUNCA cambia una `Subscription` ya
  otorgada — sus condiciones quedan copiadas (`granted_*`), no resueltas en
  vivo contra el catálogo.
* ``subscription_quota_usage`` — contador agregado por tenant/recurso/período
  (`used` committed + `reserved` en curso), con `limit_snapshot` propio.
* ``subscription_quota_reservations`` — una fila por operación
  (`operation_id`, elegido por el llamador), para que un reintento con el
  mismo id nunca reserve una unidad de más.

Cambios en ``subscriptions``
-----------------------------
* ``status`` pasa a CHECK cerrado (``TRIAL``/``ACTIVE``/``GRACE``/
  ``READ_ONLY``/``CANCELLED``) — el default ``'ACTIVE'`` ya cae adentro.
* ``current_period_start``/``current_period_end`` pasan de ``DATE`` a
  ``TIMESTAMPTZ`` — sin filas con valor hoy (nada usa períodos reales
  todavía), así que el ``USING`` es trivial.
* Columnas nuevas, todas nullable y sin backfill: ``trial_ends_at``,
  ``grace_ends_at``, ``cancel_at_period_end`` (default ``false``),
  ``granted_ia_queries_per_month``, ``granted_imports_per_month``,
  ``granted_photo_pdf_reads_per_month``.
* FK ``plan_code`` → ``plan_definitions.plan_code`` (por eso el catálogo se
  siembra ANTES de agregar la FK — una fila `FREE` ya existe cuando se valida).
* Índice único parcial ``uq_subscriptions_one_live_per_tenant`` — un tenant no
  puede tener dos suscripciones no-canceladas a la vez. Solo PostgreSQL, mismo
  criterio que ``uq_access_requests_open_email`` (SQLite lo cubre a nivel de
  aplicación en los tests).

Additive en su mayoría; el único cambio destructivo-en-apariencia es el tipo
de columna de ``current_period_start/end``, seguro porque hoy son NULL en
todas las filas existentes (nada las escribe todavía).

Idempotente (E8c)
-----------------
``upgrade()`` saltea lo que ya existe; ``downgrade()`` solo borra lo que está.
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert

from alembic import op

revision = "20260918_0001"
down_revision = "20260917_0001"
branch_labels = None
depends_on = None

_ONE_LIVE_SUBSCRIPTION_INDEX = "uq_subscriptions_one_live_per_tenant"

#: Sembrado una sola vez (ON CONFLICT DO NOTHING): si alguien ya ajustó estos
#: valores a mano después del seed inicial, una corrida futura de esta misma
#: migración (o del `preDeployCommand` corriendo dos veces) no los pisa.
_PLAN_SEED: tuple[dict[str, object], ...] = (
    {
        "plan_code": "FREE",
        "display_name": "Gratuito (legado)",
        "seats_included": 1,
        # "Sin límite mensual configurado" tal cual estaba: el único control
        # que ya tenían estas cuentas es el rate-limit diario global de
        # `agent.py`, que sigue vigente sin cambios. No es "ilimitado para
        # siempre" como decisión nueva — es no tocar el comportamiento previo.
        "ia_queries_per_month": 999999,
        "imports_per_month": 999999,
        "photo_pdf_reads_per_month": 999999,
        "price_usd_reference": None,
        "price_ars_reference": None,
        "is_offered": False,
    },
    {
        "plan_code": "esencial",
        "display_name": "Esencial",
        "seats_included": 1,
        "ia_queries_per_month": 20,
        "imports_per_month": 5,
        "photo_pdf_reads_per_month": 1,
        "price_usd_reference": "12.00",
        "price_ars_reference": "18500.00",
        "is_offered": True,
    },
    {
        "plan_code": "control",
        "display_name": "Control",
        "seats_included": 3,
        "ia_queries_per_month": 70,
        "imports_per_month": 25,
        "photo_pdf_reads_per_month": 7,
        "price_usd_reference": "27.00",
        "price_ars_reference": "41500.00",
        "is_offered": True,
    },
    {
        "plan_code": "direccion",
        "display_name": "Dirección",
        "seats_included": 5,
        "ia_queries_per_month": 180,
        "imports_per_month": 75,
        "photo_pdf_reads_per_month": 18,
        "price_usd_reference": "59.00",
        "price_ars_reference": "90500.00",
        "is_offered": True,
    },
)


def _tabla_existe(bind: sa.Connection, tabla: str) -> bool:
    return sa.inspect(bind).has_table(tabla)


def _columnas(bind: sa.Connection, tabla: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(tabla)}


def _fk_existe(bind: sa.Connection, tabla: str, columna: str) -> bool:
    return any(
        columna in fk["constrained_columns"]
        for fk in sa.inspect(bind).get_foreign_keys(tabla)
    )


def _check_existe(bind: sa.Connection, tabla: str, nombre: str) -> bool:
    if bind.dialect.name == "postgresql":
        fila = bind.execute(
            sa.text(
                "SELECT 1 FROM pg_constraint "
                "WHERE conname = :n AND conrelid = to_regclass(:t)"
            ),
            {"n": nombre, "t": tabla},
        ).first()
        return fila is not None
    try:
        return any(c.get("name") == nombre for c in sa.inspect(bind).get_check_constraints(tabla))
    except NotImplementedError:  # pragma: no cover - dialecto sin introspección
        return False


def _crear_plan_definitions(bind: sa.Connection) -> None:
    if not _tabla_existe(bind, "plan_definitions"):
        op.create_table(
            "plan_definitions",
            sa.Column("plan_code", sa.Text(), primary_key=True),
            sa.Column("display_name", sa.Text(), nullable=False),
            sa.Column("seats_included", sa.Integer(), nullable=False),
            sa.Column("ia_queries_per_month", sa.Integer(), nullable=False),
            sa.Column("imports_per_month", sa.Integer(), nullable=False),
            sa.Column("photo_pdf_reads_per_month", sa.Integer(), nullable=False),
            sa.Column("price_usd_reference", sa.Numeric(8, 2), nullable=True),
            sa.Column("price_ars_reference", sa.Numeric(14, 2), nullable=True),
            sa.Column("is_offered", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False,
                server_default=sa.func.now(),
            ),
        )

    tabla = sa.table(
        "plan_definitions",
        sa.column("plan_code", sa.Text()),
        sa.column("display_name", sa.Text()),
        sa.column("seats_included", sa.Integer()),
        sa.column("ia_queries_per_month", sa.Integer()),
        sa.column("imports_per_month", sa.Integer()),
        sa.column("photo_pdf_reads_per_month", sa.Integer()),
        sa.column("price_usd_reference", sa.Numeric(8, 2)),
        sa.column("price_ars_reference", sa.Numeric(14, 2)),
        sa.column("is_offered", sa.Boolean()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    ahora = datetime.now(UTC)
    filas = [{**fila, "created_at": ahora, "updated_at": ahora} for fila in _PLAN_SEED]
    if bind.dialect.name == "postgresql":
        sentencia = pg_insert(tabla).values(filas).on_conflict_do_nothing(
            index_elements=["plan_code"]
        )
        bind.execute(sentencia)
    else:
        for fila in filas:
            existente = bind.execute(
                sa.text("SELECT 1 FROM plan_definitions WHERE plan_code = :pc"),
                {"pc": fila["plan_code"]},
            ).first()
            if existente is None:
                bind.execute(sa.text(
                    "INSERT INTO plan_definitions (plan_code, display_name, seats_included, "
                    "ia_queries_per_month, imports_per_month, photo_pdf_reads_per_month, "
                    "price_usd_reference, price_ars_reference, is_offered, created_at, "
                    "updated_at) VALUES (:plan_code, :display_name, :seats_included, "
                    ":ia_queries_per_month, :imports_per_month, :photo_pdf_reads_per_month, "
                    ":price_usd_reference, :price_ars_reference, :is_offered, :created_at, "
                    ":updated_at)"
                ), fila)


def _alterar_subscriptions(bind: sa.Connection) -> None:
    cols = _columnas(bind, "subscriptions")
    dialecto = bind.dialect.name

    nuevas: list[sa.Column] = []
    if "trial_ends_at" not in cols:
        nuevas.append(sa.Column("trial_ends_at", sa.DateTime(timezone=True), nullable=True))
    if "grace_ends_at" not in cols:
        nuevas.append(sa.Column("grace_ends_at", sa.DateTime(timezone=True), nullable=True))
    if "cancel_at_period_end" not in cols:
        nuevas.append(
            sa.Column(
                "cancel_at_period_end", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )
    if "granted_ia_queries_per_month" not in cols:
        nuevas.append(sa.Column("granted_ia_queries_per_month", sa.Integer(), nullable=True))
    if "granted_imports_per_month" not in cols:
        nuevas.append(sa.Column("granted_imports_per_month", sa.Integer(), nullable=True))
    if "granted_photo_pdf_reads_per_month" not in cols:
        nuevas.append(
            sa.Column("granted_photo_pdf_reads_per_month", sa.Integer(), nullable=True)
        )
    for columna in nuevas:
        op.add_column("subscriptions", columna)

    # current_period_start/end: DATE -> TIMESTAMPTZ. Todas las filas existentes
    # tienen NULL acá (nada usa períodos reales todavía) — el USING es trivial.
    tipo_actual = {c["name"]: c["type"] for c in sa.inspect(bind).get_columns("subscriptions")}
    for columna in ("current_period_start", "current_period_end"):
        ya_es_timestamptz = isinstance(tipo_actual.get(columna), sa.DateTime)
        if ya_es_timestamptz:
            continue
        if dialecto == "postgresql":
            op.execute(
                f"ALTER TABLE subscriptions ALTER COLUMN {columna} TYPE TIMESTAMPTZ "
                f"USING {columna}::timestamptz"
            )
        else:
            # SQLite no tipa de verdad las columnas — no hace falta migrar el
            # storage, alcanza con que el modelo declare DateTime(timezone=True).
            pass

    if not _fk_existe(bind, "subscriptions", "plan_code"):
        if dialecto != "postgresql":
            with op.batch_alter_table("subscriptions") as batch_op:
                batch_op.create_foreign_key(
                    "fk_subscriptions_plan_code", "plan_definitions", ["plan_code"], ["plan_code"]
                )
        else:
            op.create_foreign_key(
                "fk_subscriptions_plan_code",
                "subscriptions",
                "plan_definitions",
                ["plan_code"],
                ["plan_code"],
            )

    if not _check_existe(bind, "subscriptions", "ck_subscriptions_status"):
        predicado = "status IN ('TRIAL','ACTIVE','GRACE','READ_ONLY','CANCELLED')"
        if dialecto != "postgresql":
            with op.batch_alter_table("subscriptions") as batch_op:
                batch_op.create_check_constraint("ck_subscriptions_status", predicado)
        else:
            op.execute(
                f"ALTER TABLE subscriptions ADD CONSTRAINT ck_subscriptions_status "
                f"CHECK ({predicado}) NOT VALID"
            )
            op.execute(
                "ALTER TABLE subscriptions VALIDATE CONSTRAINT ck_subscriptions_status"
            )

    if dialecto == "postgresql":
        op.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {_ONE_LIVE_SUBSCRIPTION_INDEX} "
            "ON subscriptions (tenant_id) WHERE status <> 'CANCELLED'"
        )


def _crear_tablas_de_cupo(bind: sa.Connection) -> None:
    if not _tabla_existe(bind, "subscription_quota_usage"):
        op.create_table(
            "subscription_quota_usage",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "tenant_id", UUID(as_uuid=True),
                sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column(
                "subscription_id", UUID(as_uuid=True),
                sa.ForeignKey("subscriptions.subscription_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("resource", sa.Text(), nullable=False),
            sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
            sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
            sa.Column("used", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("reserved", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("limit_snapshot", sa.Integer(), nullable=False),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False,
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint(
                "tenant_id", "resource", "period_start",
                name="uq_quota_usage_tenant_resource_period",
            ),
            sa.CheckConstraint(
                "resource IN ('ia_query','import','photo_pdf_read')",
                name="ck_quota_usage_resource",
            ),
            sa.CheckConstraint("used >= 0", name="ck_quota_usage_used_non_negative"),
            sa.CheckConstraint("reserved >= 0", name="ck_quota_usage_reserved_non_negative"),
        )
        op.create_index(
            "ix_quota_usage_tenant_id", "subscription_quota_usage", ["tenant_id"]
        )

    if not _tabla_existe(bind, "subscription_quota_reservations"):
        op.create_table(
            "subscription_quota_reservations",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "tenant_id", UUID(as_uuid=True),
                sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column("resource", sa.Text(), nullable=False),
            sa.Column("operation_id", sa.Text(), nullable=False),
            sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
            sa.Column("units", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("state", sa.Text(), nullable=False, server_default="RESERVED"),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False,
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint(
                "tenant_id", "resource", "operation_id", name="uq_quota_reservation_operation"
            ),
            sa.CheckConstraint(
                "resource IN ('ia_query','import','photo_pdf_read')",
                name="ck_quota_reservation_resource",
            ),
            sa.CheckConstraint(
                "state IN ('RESERVED','COMMITTED','RELEASED')",
                name="ck_quota_reservation_state",
            ),
            sa.CheckConstraint("units > 0", name="ck_quota_reservation_units_positive"),
        )
        op.create_index(
            "ix_quota_reservation_tenant_id", "subscription_quota_reservations", ["tenant_id"]
        )


def upgrade() -> None:
    bind = op.get_bind()
    _crear_plan_definitions(bind)
    _alterar_subscriptions(bind)
    _crear_tablas_de_cupo(bind)


def downgrade() -> None:
    bind = op.get_bind()
    if _tabla_existe(bind, "subscription_quota_reservations"):
        op.drop_table("subscription_quota_reservations")
    if _tabla_existe(bind, "subscription_quota_usage"):
        op.drop_table("subscription_quota_usage")

    if bind.dialect.name == "postgresql":
        op.execute(f"DROP INDEX IF EXISTS {_ONE_LIVE_SUBSCRIPTION_INDEX}")
        if _check_existe(bind, "subscriptions", "ck_subscriptions_status"):
            op.execute(
                "ALTER TABLE subscriptions DROP CONSTRAINT ck_subscriptions_status"
            )
        if _fk_existe(bind, "subscriptions", "plan_code"):
            op.drop_constraint(
                "fk_subscriptions_plan_code", "subscriptions", type_="foreignkey"
            )
        op.execute(
            "ALTER TABLE subscriptions ALTER COLUMN current_period_start TYPE DATE "
            "USING current_period_start::date"
        )
        op.execute(
            "ALTER TABLE subscriptions ALTER COLUMN current_period_end TYPE DATE "
            "USING current_period_end::date"
        )
    else:
        with op.batch_alter_table("subscriptions") as batch_op:
            if _check_existe(bind, "subscriptions", "ck_subscriptions_status"):
                batch_op.drop_constraint("ck_subscriptions_status", type_="check")
            if _fk_existe(bind, "subscriptions", "plan_code"):
                batch_op.drop_constraint("fk_subscriptions_plan_code", type_="foreignkey")

    cols = _columnas(bind, "subscriptions")
    for columna in (
        "granted_photo_pdf_reads_per_month",
        "granted_imports_per_month",
        "granted_ia_queries_per_month",
        "cancel_at_period_end",
        "grace_ends_at",
        "trial_ends_at",
    ):
        if columna in cols:
            op.drop_column("subscriptions", columna)

    if _tabla_existe(bind, "plan_definitions"):
        op.drop_table("plan_definitions")
