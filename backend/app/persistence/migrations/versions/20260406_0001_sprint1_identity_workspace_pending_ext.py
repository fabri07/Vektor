"""sprint1_identity_workspace_pending_ext

Revision ID: 20260406_0001
Revises: 20260401_0003
Create Date: 2026-04-06 00:00:00.000000

Sprint 1 — Auth social + Google Workspace:

Tablas nuevas:
  - user_auth_identities               (identidad OIDC por proveedor social)
  - user_google_workspace_connections  (credenciales API de Google Workspace)

Extensión de pending_actions:
  - execution_status         (ciclo de vida de ejecución externa)
  - approved_at
  - failure_code / failure_message
  - idempotency_key          (unicidad de ejecución externa, UNIQUE parcial)
  - external_system          (e.g. GOOGLE_GMAIL, GOOGLE_DRIVE)

RLS:
  - Ambas tablas nuevas tienen tenant_id y política RLS idéntica al resto del proyecto.
  - Decisión: aunque los datos son conceptualmente "por usuario", incluir tenant_id
    mantiene el aislamiento DB-level uniforme y protege contra lecturas cross-tenant.

Backfill de users.google_* → user_google_workspace_connections:
  - Las columnas google_* en users fueron agregadas en 20260401_0003 como parte de
    un flujo de Google Workspace que estaba en construcción y nunca llegó a producción.
  - No existe ningún usuario con tokens Google reales en ambientes productivos.
  - Decisión: NO se hace backfill. Las columnas quedan nullable/deprecadas en users.
    Si en el futuro se detectan datos en esas columnas, migrarlos con un script ad-hoc.
  - Esta decisión está registrada aquí para que sea auditable.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260406_0001"
down_revision: Union[str, None] = "20260401_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 1. CREATE TABLE user_auth_identities ──────────────────────────────────
    # Identidad OIDC por proveedor social (login/registro).
    # tenant_id incluido para RLS uniforme con el resto del proyecto.
    op.create_table(
        "user_auth_identities",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("provider_subject", sa.Text(), nullable=False),
        sa.Column("provider_email", sa.Text(), nullable=False),
        sa.Column(
            "linked_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "provider", "provider_subject",
            name="uq_auth_identity_provider_subject",
        ),
    )
    op.create_index("idx_auth_identities_tenant", "user_auth_identities", ["tenant_id"])
    op.create_index("idx_auth_identities_user", "user_auth_identities", ["user_id"])
    op.create_index("idx_auth_identities_provider", "user_auth_identities", ["provider"])

    # ── 2. CREATE TABLE user_google_workspace_connections ─────────────────────
    # Credenciales de Google Workspace API por usuario.
    # tenant_id incluido para RLS uniforme con el resto del proyecto.
    op.create_table(
        "user_google_workspace_connections",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.user_id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("access_token_encrypted", sa.Text(), nullable=False),
        sa.Column("refresh_token_encrypted", sa.Text(), nullable=False),
        sa.Column(
            "scopes_granted",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("ARRAY[]::TEXT[]"),
        ),
        sa.Column(
            "connected_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_refresh_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.Text(), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.UniqueConstraint("user_id", name="uq_workspace_connection_user"),
    )
    op.create_index(
        "idx_workspace_connections_tenant",
        "user_google_workspace_connections",
        ["tenant_id"],
    )

    # ── 3. RLS en ambas tablas nuevas ─────────────────────────────────────────
    for tbl in ("user_auth_identities", "user_google_workspace_connections"):
        op.execute(f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY {tbl}_tenant ON {tbl}
            USING (tenant_id = current_setting('app.current_tenant_id', TRUE)::uuid)
            """
        )

    # ── 4. ALTER TABLE pending_actions — campos de ejecución externa ──────────
    op.execute(
        "ALTER TABLE pending_actions "
        "ADD COLUMN IF NOT EXISTS execution_status TEXT NOT NULL DEFAULT 'NOT_STARTED'"
    )
    op.execute(
        """
        ALTER TABLE pending_actions
        ADD CONSTRAINT ck_pending_actions_execution_status
        CHECK (execution_status IN (
            'NOT_STARTED', 'IN_PROGRESS', 'SUCCEEDED', 'FAILED', 'REQUIRES_RECONNECT'
        ))
        """
    )
    op.execute(
        "ALTER TABLE pending_actions ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ"
    )
    op.execute(
        "ALTER TABLE pending_actions ADD COLUMN IF NOT EXISTS failure_code TEXT"
    )
    op.execute(
        "ALTER TABLE pending_actions ADD COLUMN IF NOT EXISTS failure_message TEXT"
    )
    op.execute(
        "ALTER TABLE pending_actions ADD COLUMN IF NOT EXISTS idempotency_key TEXT"
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes
            WHERE tablename = 'pending_actions'
              AND indexname = 'uq_pending_actions_idempotency_key'
          ) THEN
            CREATE UNIQUE INDEX uq_pending_actions_idempotency_key
            ON pending_actions (idempotency_key)
            WHERE idempotency_key IS NOT NULL;
          END IF;
        END $$;
        """
    )
    op.execute(
        "ALTER TABLE pending_actions ADD COLUMN IF NOT EXISTS external_system TEXT"
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_pending_external_retry
        ON pending_actions (tenant_id, execution_status)
        WHERE external_system IS NOT NULL
        """
    )


def downgrade() -> None:
    # Revertir pending_actions
    op.execute("DROP INDEX IF EXISTS idx_pending_external_retry")
    op.execute(
        "ALTER TABLE pending_actions DROP CONSTRAINT IF EXISTS ck_pending_actions_execution_status"
    )
    op.execute("DROP INDEX IF EXISTS uq_pending_actions_idempotency_key")
    for col in ("external_system", "idempotency_key", "failure_message",
                "failure_code", "approved_at", "execution_status"):
        op.drop_column("pending_actions", col)

    # Revertir RLS
    for tbl in ("user_auth_identities", "user_google_workspace_connections"):
        op.execute(f"DROP POLICY IF EXISTS {tbl}_tenant ON {tbl}")
        op.execute(f"ALTER TABLE {tbl} DISABLE ROW LEVEL SECURITY")

    # Revertir tablas nuevas
    op.drop_index("idx_workspace_connections_tenant",
                  table_name="user_google_workspace_connections")
    op.drop_index("idx_auth_identities_provider", table_name="user_auth_identities")
    op.drop_index("idx_auth_identities_user", table_name="user_auth_identities")
    op.drop_index("idx_auth_identities_tenant", table_name="user_auth_identities")
    op.drop_table("user_google_workspace_connections")
    op.drop_table("user_auth_identities")
