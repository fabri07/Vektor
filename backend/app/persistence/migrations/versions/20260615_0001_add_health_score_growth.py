"""Stage 5a: agrega score_growth a health_score_snapshots

Revision ID: 20260615_0001
Revises: 20260601_0001
Create Date: 2026-06-15

Contexto
--------
Migración ADDITIVE para la fórmula v2 del score de salud (Stage 5a).

• score_growth IS NULL  → snapshot v1
  (fórmula: cash×0.30 + margin×0.30 + stock×0.25 + supplier×0.15)
• score_growth IS NOT NULL → snapshot v2
  (fórmula: cash×0.30 + stock×0.20 + supplier×0.10 + margin×0.20 + growth×0.20)

Nota sobre score_margin
-----------------------
La columna score_margin siempre almacenó el score de MARGEN calculado por health_engine.py.
Antes de Stage 5a, agents/health/agent.py leía score_margin y lo etiquetaba erróneamente como
'discipline_score' en la respuesta al usuario. Stage 5a corrige esa interpretación.
No se modifica ni renombra score_margin — su significado real nunca cambió.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260615_0001"
down_revision = "20260601_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "health_score_snapshots",
        sa.Column(
            "score_growth",
            sa.Integer(),
            nullable=True,
            comment="Score crecimiento de ventas 0-100. NULL = snapshot v1 (pre-Stage-5a).",
        ),
    )


def downgrade() -> None:
    op.drop_column("health_score_snapshots", "score_growth")
