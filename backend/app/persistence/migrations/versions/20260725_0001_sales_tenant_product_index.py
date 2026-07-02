"""Índice (tenant_id, product_id) en sales_entries para FactsProvider.

Revision ID: 20260725_0001
Revises: 20260724_0002
Create Date: 2026-07-25

Contexto
--------
Migración ADDITIVE — FactsService fase 1. `facts_provider.load_facts_frames`
deriva `last_sold_date` con `max(transaction_date) GROUP BY product_id` sobre
el histórico completo del tenant, en cada snapshot de métricas. Sin índice de
agrupación, en tenants con >10k ventas eso es un aggregate sin soporte por
request. Este índice compuesto cubre el patrón (filtro por tenant + group by
product_id).

``downgrade`` dropea el índice (sin datos, totalmente reversible).
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260725_0001"
down_revision = "20260724_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_sales_entries_tenant_product",
        "sales_entries",
        ["tenant_id", "product_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_sales_entries_tenant_product", table_name="sales_entries")
