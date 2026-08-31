"""Bloque 2 — tabla product_supplier_links (Tienda → proveedor)

Revision ID: 20260831_0001
Revises: 20260826_0001
Create Date: 2026-08-31

Contexto
--------
Migración ADDITIVE — tabla nueva, sin tocar ninguna existente.

Diagnóstico real contra Asteria (2026-08-30): 0 proveedores existen en el
tenant y hoy no hay relación Producto↔Proveedor persistente y declarativa (solo
`inventory_movements.supplier_id`, evidencia transaccional puntual de una
compra). Un producto real del archivo ("ganchos para cortina de baño") se
repuso desde DOS tiendas distintas ('El pasillo' y 'sublink') — una FK simple
`Product.supplier_id` perdería esa segunda fuente, así que la relación es
muchos-a-muchos, no 1:1.

`source` distingue una declaración de catálogo (sin evidencia de compra) de un
vínculo respaldado por evidencia real, para que "Tienda → proveedor" nunca se
confunda con "esto se compró acá". `source_upload_id`/`source_context_id` dan
la procedencia que una relectura necesita para revertir solo lo que ELLA trajo
(mismo criterio que `sales_entries.source_upload_id`); `voided_at` es
soft-delete, no hard-delete, para poder auditar/revertir.

Gateado por `PRODUCT_SUPPLIER_LINKS_ROLLOUT_TENANT_IDS` (lista vacía por
defecto — ningún tenant se ve afectado hasta habilitarse uno por vez), mismo
criterio que el motor de costos de compra (F-H6.c/d).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260831_0001"
down_revision = "20260826_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "product_supplier_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("supplier_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("source_upload_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_context_id", sa.String(length=200), nullable=True),
        sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["supplier_id"], ["suppliers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_upload_id"], ["uploaded_files.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "tenant_id",
            "product_id",
            "supplier_id",
            name="uq_product_supplier_links_tenant_product_supplier",
        ),
        sa.CheckConstraint(
            "source IN ('catalog_declared', 'purchase_evidence')",
            name="ck_product_supplier_links_source",
        ),
    )
    op.create_index(
        "ix_product_supplier_links_tenant_id", "product_supplier_links", ["tenant_id"]
    )
    op.create_index(
        "ix_product_supplier_links_product_id", "product_supplier_links", ["product_id"]
    )
    op.create_index(
        "ix_product_supplier_links_source_upload_id",
        "product_supplier_links",
        ["source_upload_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_product_supplier_links_source_upload_id", table_name="product_supplier_links")
    op.drop_index("ix_product_supplier_links_product_id", table_name="product_supplier_links")
    op.drop_index("ix_product_supplier_links_tenant_id", table_name="product_supplier_links")
    op.drop_table("product_supplier_links")
