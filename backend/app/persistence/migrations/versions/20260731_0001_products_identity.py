"""Identidad de producto (Fase 2, T1) — columnas normalizadas + match_candidates

Revision ID: 20260731_0001
Revises: 20260730_0001
Create Date: 2026-07-31

Contexto
--------
Migración ADDITIVE — base persistida de la Fase 2 de identidad de producto.
Hoy la identidad de un producto (para matchear ventas/gastos/imports contra
el catálogo del tenant) es 100% application-level y frágil. Esta migración
agrega la base: un campo raw ``barcode`` + 4 columnas normalizadas
independientes (``barcode_normalized``, ``sku_normalized``, ``name_normalized``,
``brand_normalized`` — decisión del usuario: claves independientes por campo,
NO una sola clave jerárquica excluyente) más ``expiry_date`` (informativa,
se usa recién en F6). Las 4 columnas ``*_normalized`` las llena un listener
SQLAlchemy ``before_insert``/``before_update`` en ``Product`` (fuente única de
cálculo) — esta migración solo abre el espacio en el schema.

Índices de BÚSQUEDA por ``(tenant_id, *_normalized)`` — NO únicos, la
unicidad es Fase 5, todavía no se resuelve el lookup/dedupe (eso es T2).

También agrega ``unclassified_records.match_candidates`` (JSONB, nullable):
candidatos de match para filas ambiguas que arme el resolver de T2, forma
``[{id, matched_by, name, sku, barcode}]``.

Todas las columnas nullable — filas existentes quedan con ``NULL`` (el
listener las llena recién en el próximo insert/update de cada fila).
``downgrade`` simétrico.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260731_0001"
down_revision = "20260730_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("products", sa.Column("barcode", sa.String(length=64), nullable=True))
    op.add_column(
        "products", sa.Column("barcode_normalized", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "products", sa.Column("sku_normalized", sa.String(length=120), nullable=True)
    )
    op.add_column(
        "products", sa.Column("name_normalized", sa.String(length=400), nullable=True)
    )
    op.add_column(
        "products", sa.Column("brand_normalized", sa.String(length=200), nullable=True)
    )
    op.add_column("products", sa.Column("expiry_date", sa.Date(), nullable=True))

    op.create_index(
        "ix_products_tenant_barcode_norm", "products", ["tenant_id", "barcode_normalized"]
    )
    op.create_index(
        "ix_products_tenant_sku_norm", "products", ["tenant_id", "sku_normalized"]
    )
    op.create_index(
        "ix_products_tenant_name_norm", "products", ["tenant_id", "name_normalized"]
    )

    op.add_column(
        "unclassified_records",
        sa.Column(
            "match_candidates",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("unclassified_records", "match_candidates")

    op.drop_index("ix_products_tenant_name_norm", table_name="products")
    op.drop_index("ix_products_tenant_sku_norm", table_name="products")
    op.drop_index("ix_products_tenant_barcode_norm", table_name="products")

    op.drop_column("products", "expiry_date")
    op.drop_column("products", "brand_normalized")
    op.drop_column("products", "name_normalized")
    op.drop_column("products", "sku_normalized")
    op.drop_column("products", "barcode_normalized")
    op.drop_column("products", "barcode")
