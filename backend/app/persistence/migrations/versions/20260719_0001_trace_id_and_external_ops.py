"""trace_id transversal + tabla unificada external_operation_logs

- decision_audit_log + communication_log ganan `trace_id` (correlación).
- communication_log gana `provider_message_id`.
- nueva tabla external_operation_logs (log unificado de llamadas a proveedores).

Additive / reversible.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "20260719_0001"
down_revision = "20260718_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "decision_audit_log",
        sa.Column("trace_id", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_decision_audit_log_trace", "decision_audit_log", ["trace_id"]
    )

    op.add_column(
        "communication_log",
        sa.Column("trace_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "communication_log",
        sa.Column("provider_message_id", sa.String(length=200), nullable=True),
    )

    op.create_table(
        "external_operation_logs",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), nullable=True),
        sa.Column("operation_type", sa.String(length=40), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("provider_account_id", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("request_payload_redacted", JSONB, nullable=True),
        sa.Column("response_payload_redacted", JSONB, nullable=True),
        sa.Column("provider_request_id", sa.String(length=200), nullable=True),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_external_operation_logs_tenant", "external_operation_logs", ["tenant_id"]
    )
    op.create_index(
        "ix_external_operation_logs_trace", "external_operation_logs", ["trace_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_external_operation_logs_trace", table_name="external_operation_logs")
    op.drop_index("ix_external_operation_logs_tenant", table_name="external_operation_logs")
    op.drop_table("external_operation_logs")

    op.drop_column("communication_log", "provider_message_id")
    op.drop_column("communication_log", "trace_id")

    op.drop_index("ix_decision_audit_log_trace", table_name="decision_audit_log")
    op.drop_column("decision_audit_log", "trace_id")
