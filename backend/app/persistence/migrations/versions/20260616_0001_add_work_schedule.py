"""Sprint 20: agrega días y horarios laborales a business_profiles

Revision ID: 20260616_0001
Revises: 20260615_0001
Create Date: 2026-06-16

Contexto
--------
Migración ADDITIVE para configurar días y horario laboral por tenant.

• work_days: array JSONB de int 0-6 (0=lunes … 6=domingo).
• work_open_hour / work_close_hour: hora 0-23.

Las 3 columnas son nullable. NULL = no configurado → el WorkScheduleService sirve
defaults (Lun-Sáb 09-18). Cuentas existentes quedan en NULL hasta que configuren.
Sirve para destacar el botón de cierre de caja al final del día laboral.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260616_0001"
down_revision = "20260615_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "business_profiles",
        sa.Column(
            "work_days",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="Días laborales: array int 0-6 (0=lunes). NULL = no configurado.",
        ),
    )
    op.add_column(
        "business_profiles",
        sa.Column(
            "work_open_hour",
            sa.Integer(),
            nullable=True,
            comment="Hora de apertura 0-23. NULL = no configurado (default 9).",
        ),
    )
    op.add_column(
        "business_profiles",
        sa.Column(
            "work_close_hour",
            sa.Integer(),
            nullable=True,
            comment="Hora de cierre 0-23. NULL = no configurado (default 18).",
        ),
    )


def downgrade() -> None:
    op.drop_column("business_profiles", "work_close_hour")
    op.drop_column("business_profiles", "work_open_hour")
    op.drop_column("business_profiles", "work_days")
