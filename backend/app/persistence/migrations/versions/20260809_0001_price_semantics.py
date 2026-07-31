"""Separar los tres precios de un producto y el precio realmente vendido.

Hasta acá un producto tenía dos columnas de precio (``sale_price_ars`` y
``unit_cost_ars``) y la venta ninguna. Eso obligaba a meter tres conceptos
distintos en el mismo campo, y el mapeo de ingesta terminaba eligiendo uno por
orden de procesamiento — la forma más silenciosa de inventar un dato.

La separación correcta:

* ``products.unit_cost_ars``  *(ya existía)* — costo unitario vigente o de referencia.
* ``products.list_price_ars`` **(nueva)**    — precio sugerido por proveedor/lista.
* ``products.sale_price_ars`` *(ya existía)* — precio de venta vigente que configuró
  el negocio. Sigue NOT NULL y sigue siendo el ÚNICO que entra al margen.
* ``sales_entries.unit_price`` **(nueva)**   — precio realmente vendido en ESA
  transacción.

La última es la que faltaba de verdad: el precio histórico real no puede vivir
únicamente en ``products``, porque cambia por descuento, fecha, canal o cliente.
``products`` describe la configuración vigente; la transacción guarda lo que
efectivamente pasó.

Additive y nullable, sin backfill. En particular ``unit_price`` NO se rellena con
``amount / quantity``: esa división produciría un número plausible pero inventado
(no sabemos si el amount es unitario o total en las filas históricas). NULL es lo
honesto — "no se registró" — y se llena solo cuando un archivo lo trae mapeado.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260809_0001"
down_revision = "20260808_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column("list_price_ars", sa.Numeric(12, 2), nullable=True),
    )
    op.add_column(
        "sales_entries",
        sa.Column("unit_price", sa.Numeric(12, 2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sales_entries", "unit_price")
    op.drop_column("products", "list_price_ars")
