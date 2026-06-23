"""custom_fields JSONB en social_metrics (columnas propias en Marketing)

Revision ID: 20260722_0001
Revises: 20260721_0001
Create Date: 2026-07-22

Contexto
--------
Migración ADDITIVE — habilita columnas personalizadas en la sección Marketing,
espejando el resto de las secciones (Ventas, Gastos, Productos, Clientes,
Proveedores). ``social_metrics`` es la tabla de métricas de redes/inversión
publicitaria cargadas manualmente; le agregamos ``custom_fields`` JSONB para que
el tenant defina y complete columnas propias (mismo mecanismo de
``vertical_field_definitions``/``tenant_custom_field_definitions`` con
``entity_type='marketing'``).

NOT NULL con ``server_default '{}'::jsonb`` — las filas existentes quedan con
diccionario vacío. ``downgrade`` simétrico.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260722_0001"
down_revision = "20260721_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "social_metrics",
        sa.Column(
            "custom_fields",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("social_metrics", "custom_fields")
