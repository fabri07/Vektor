"""Tablas access_requests + access_request_tokens (registro cerrado).

Revision ID: 20260806_0001
Revises: 20260805_0001
Create Date: 2026-08-06

Contexto
--------
Se cierra el registro abierto: el visitante ya no crea una cuenta, manda una
SOLICITUD que el dueño revisa a mano. Estas dos tablas son públicas (sin
``tenant_id``), igual que ``contact_leads``: las genera un visitante anónimo
antes de que exista ningún tenant.

Additive puro y aditivo a la cadena: no toca ninguna tabla existente. Idempotente
vía ``sa.inspect`` (varios servicios de Railway pueden correr
``alembic upgrade head`` en paralelo en un mismo deploy).

CHECKs — no son decorativos
---------------------------
Son la garantía a nivel DB de dos decisiones de producto que un bug de aplicación
no debe poder violar:

* ``ck_access_requests_vertical_other_text`` + ``ck_access_requests_assigned_
  vertical_code``: declarar el rubro ``'otros'`` obliga a explicarlo, y ``'otros'``
  es INESCRIBIBLE como vertical asignado (solo los 3 operativos).
* ``ck_access_requests_approved_needs_vertical``: una solicitud aprobada no puede
  quedarse sin vertical asignado.

Los literales van hardcodeados a propósito: una migración es una foto del pasado,
no sigue la evolución del código. El espejo vivo está derivado de los enums en
``AccessRequest.__table_args__``; si cambia uno, cambia el otro con una migración
nueva.

``requested_plan`` va NOT NULL **sin server_default ni backfill**: la tabla es
nueva y el plan solicitado siempre lo elige el usuario (no hay valor neutro
razonable). Es intención declarada, NO una suscripción — al aprobar se crea
siempre una suscripción FREE.

Índice único parcial
--------------------
``uq_access_requests_open_email`` (un solo trámite abierto por email, sobre
``lower(email)``) se crea SOLO en PostgreSQL: SQLite ignoraría matices del
predicado y en los tests la unicidad la cubre la dedup de aplicación. Misma
convención que ``uq_customers_dni_per_tenant``. La tabla se acaba de crear y está
vacía, así que no hace falta ``CONCURRENTLY``.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "20260806_0001"
down_revision: str | None = "20260805_0001"
branch_labels: str | None = None
depends_on: str | None = None

_OPEN_EMAIL_INDEX = "uq_access_requests_open_email"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    if "access_requests" not in existing:
        op.create_table(
            "access_requests",
            sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
            # Contacto
            sa.Column("full_name", sa.String(length=200), nullable=False),
            sa.Column("email", sa.String(length=255), nullable=False),
            sa.Column("phone", sa.String(length=50), nullable=True),
            sa.Column("business_name", sa.String(length=200), nullable=False),
            # Rubro declarado (acepta 'otros')
            sa.Column("requested_vertical", sa.String(length=40), nullable=False),
            sa.Column("vertical_other_text", sa.Text(), nullable=True),
            # Intención de plan (no es una suscripción)
            sa.Column("requested_plan", sa.String(length=20), nullable=False),
            # Screening del negocio
            sa.Column("years_operating", sa.String(length=20), nullable=False),
            sa.Column("staff_size", sa.String(length=20), nullable=False),
            sa.Column("monthly_revenue_band", sa.String(length=20), nullable=False),
            sa.Column("main_concern", sa.String(length=10), nullable=False),
            sa.Column("records_format", sa.String(length=20), nullable=False),
            sa.Column("history_depth", sa.String(length=20), nullable=False),
            sa.Column("can_share_files", sa.String(length=20), nullable=False),
            sa.Column("records_notes", sa.Text(), nullable=True),
            sa.Column("applicant_notes", sa.Text(), nullable=True),
            # Estado + doble opt-in
            sa.Column(
                "status",
                sa.String(length=20),
                nullable=False,
                server_default="unverified",
            ),
            sa.Column(
                "email_verified_at", sa.DateTime(timezone=True), nullable=True
            ),
            # Revisión manual
            sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "reviewed_by_user_id",
                sa.UUID(as_uuid=True),
                sa.ForeignKey("users.user_id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("reviewed_via", sa.String(length=10), nullable=True),
            sa.Column("review_notes", sa.Text(), nullable=True),
            sa.Column("rejection_reason", sa.Text(), nullable=True),
            # Resultado de la aprobación
            sa.Column("assigned_vertical_code", sa.String(length=40), nullable=True),
            sa.Column(
                "approved_tenant_id",
                sa.UUID(as_uuid=True),
                sa.ForeignKey("tenants.tenant_id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "approved_user_id",
                sa.UUID(as_uuid=True),
                sa.ForeignKey("users.user_id", ondelete="SET NULL"),
                nullable=True,
            ),
            # Estado de los tres emails del flujo
            sa.Column(
                "verification_email_status",
                sa.String(length=20),
                nullable=False,
                server_default="pending",
            ),
            sa.Column(
                "owner_notification_status",
                sa.String(length=20),
                nullable=False,
                server_default="pending",
            ),
            sa.Column(
                "decision_email_status",
                sa.String(length=20),
                nullable=False,
                server_default="pending",
            ),
            # Consentimiento (Ley 25.326)
            sa.Column("consent_version", sa.String(length=20), nullable=False),
            sa.Column(
                "consent_accepted_at", sa.DateTime(timezone=True), nullable=False
            ),
            # Trazabilidad / anti-abuso
            sa.Column("ip_hash", sa.String(length=64), nullable=True),
            sa.Column("cta_source", sa.String(length=60), nullable=True),
            sa.Column("google_subject", sa.String(length=255), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.CheckConstraint(
                "status IN ('unverified', 'pending', 'waitlist', 'approved', "
                "'rejected', 'expired')",
                name="ck_access_requests_status",
            ),
            sa.CheckConstraint(
                "requested_plan IN ('free', 'premium')",
                name="ck_access_requests_requested_plan",
            ),
            sa.CheckConstraint(
                "requested_vertical <> 'otros' OR vertical_other_text IS NOT NULL",
                name="ck_access_requests_vertical_other_text",
            ),
            sa.CheckConstraint(
                "assigned_vertical_code IS NULL OR assigned_vertical_code IN "
                "('kiosco_almacen', 'decoracion_hogar', 'limpieza')",
                name="ck_access_requests_assigned_vertical_code",
            ),
            sa.CheckConstraint(
                "status <> 'approved' OR assigned_vertical_code IS NOT NULL",
                name="ck_access_requests_approved_needs_vertical",
            ),
        )
        op.create_index("ix_access_requests_email", "access_requests", ["email"])
        op.create_index("ix_access_requests_status", "access_requests", ["status"])
        # Cola de revisión: premium primero, después la verificada más antigua.
        op.create_index(
            "ix_access_requests_review_queue",
            "access_requests",
            ["status", "requested_plan", "email_verified_at"],
        )
        # Un solo trámite abierto por email. Parcial + sobre lower(email): solo
        # en PostgreSQL (ver docstring).
        if bind.dialect.name == "postgresql":
            op.execute(
                f"CREATE UNIQUE INDEX {_OPEN_EMAIL_INDEX} "
                "ON access_requests (lower(email)) "
                "WHERE status IN ('unverified', 'pending', 'waitlist')"
            )

    if "access_request_tokens" not in existing:
        op.create_table(
            "access_request_tokens",
            sa.Column("token_id", sa.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "access_request_id",
                sa.UUID(as_uuid=True),
                sa.ForeignKey("access_requests.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column(
                "used", sa.Boolean(), nullable=False, server_default=sa.false()
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
        )
        op.create_index(
            "ix_access_request_tokens_access_request_id",
            "access_request_tokens",
            ["access_request_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    if "access_request_tokens" in existing:
        op.drop_index(
            "ix_access_request_tokens_access_request_id",
            table_name="access_request_tokens",
        )
        op.drop_table("access_request_tokens")

    if "access_requests" in existing:
        if bind.dialect.name == "postgresql":
            op.execute(f"DROP INDEX IF EXISTS {_OPEN_EMAIL_INDEX}")
        op.drop_index(
            "ix_access_requests_review_queue", table_name="access_requests"
        )
        op.drop_index("ix_access_requests_status", table_name="access_requests")
        op.drop_index("ix_access_requests_email", table_name="access_requests")
        op.drop_table("access_requests")
