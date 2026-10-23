"""Arqueo completo en cash_closes + condición fiscal en business_profiles

Revision ID: 20260715_0001
Revises: 20260712_0001
Create Date: 2026-07-15

Contexto
--------
Migración ADDITIVE. El cierre de caja pasa de "contado vs esperado" a un arqueo
estructurado: fondo de caja inicial (no es ganancia del día), conteo de
efectivo por denominación, vales de gastos pagados desde caja sin registrar,
y resultado canónico (BALANCED / SURPLUS / SHORTAGE). Las filas existentes
quedan con NULL en los campos nuevos (cierres pre-arqueo).

`business_profiles.fiscal_condition` ('registered' | 'informal' | NULL) solo
adapta la guía del arqueo en la UI — no cambia ningún cálculo.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260715_0001"
down_revision = "20260712_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cash_closes",
        sa.Column("opening_float_ars", sa.Numeric(14, 2), nullable=True),
    )
    op.add_column(
        "cash_closes",
        sa.Column("cash_denominations", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "cash_closes",
        sa.Column("voucher_expenses_ars", sa.Numeric(14, 2), nullable=True),
    )
    op.add_column(
        "cash_closes",
        sa.Column("result_code", sa.String(10), nullable=True),
    )
    op.create_check_constraint(
        "ck_cash_closes_result_code",
        "cash_closes",
        "result_code IS NULL OR result_code IN ('BALANCED', 'SURPLUS', 'SHORTAGE')",
    )

    op.add_column(
        "business_profiles",
        sa.Column("fiscal_condition", sa.String(12), nullable=True),
    )
    op.create_check_constraint(
        "ck_business_profiles_fiscal_condition",
        "business_profiles",
        "fiscal_condition IS NULL OR fiscal_condition IN ('registered', 'informal')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_business_profiles_fiscal_condition", "business_profiles", type_="check"
    )
    op.drop_column("business_profiles", "fiscal_condition")
    op.drop_constraint("ck_cash_closes_result_code", "cash_closes", type_="check")
    op.drop_column("cash_closes", "result_code")
    op.drop_column("cash_closes", "voucher_expenses_ars")
    op.drop_column("cash_closes", "cash_denominations")
    op.drop_column("cash_closes", "opening_float_ars")
