"""sprint3_workspace_gateway

Revision ID: 20260406_0002
Revises: 20260406_0001
Create Date: 2026-04-06 00:00:01.000000

Sprint 3 — Google Workspace Gateway:

Cambios en user_google_workspace_connections:
  1. ADD google_account_email VARCHAR(320) NULL
     → email de la cuenta Google conectada (puede diferir del email de login).
       Poblado desde el userinfo endpoint durante el connect/exchange.

  2. ALTER access_token_encrypted → NULL permitido
     → necesario para la semántica de soft revoke: en disconnect() se anulan
       los tokens cifrados para minimizar la retención de credenciales.

  3. ALTER refresh_token_encrypted → NULL permitido
     → misma razón que access_token. También puede ser NULL si Google no
       devuelve refresh_token en un re-consentimiento sin revoke previo.

Decisión de retención:
  La fila NO se borra en disconnect() — preserva auditoría (connected_at,
  revoked_at, scopes_granted). Solo se anulan los tokens cifrados.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260406_0002"
down_revision: Union[str, None] = "20260406_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Agregar google_account_email
    op.add_column(
        "user_google_workspace_connections",
        sa.Column("google_account_email", sa.String(320), nullable=True),
    )

    # 2. Hacer nullable access_token_encrypted (soft revoke)
    op.alter_column(
        "user_google_workspace_connections",
        "access_token_encrypted",
        existing_type=sa.Text(),
        nullable=True,
    )

    # 3. Hacer nullable refresh_token_encrypted (soft revoke + ausencia en re-consentimiento)
    op.alter_column(
        "user_google_workspace_connections",
        "refresh_token_encrypted",
        existing_type=sa.Text(),
        nullable=True,
    )


def downgrade() -> None:
    # Revertir nullable (requiere que no haya NULLs en la tabla)
    op.alter_column(
        "user_google_workspace_connections",
        "refresh_token_encrypted",
        existing_type=sa.Text(),
        nullable=False,
    )
    op.alter_column(
        "user_google_workspace_connections",
        "access_token_encrypted",
        existing_type=sa.Text(),
        nullable=False,
    )
    op.drop_column("user_google_workspace_connections", "google_account_email")
