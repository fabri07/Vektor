"""``products.internal_sku`` — el código propio de Véktor

Revision ID: 20260903_0001
Revises: 20260902_0001
Create Date: 2026-09-03

Contexto
--------
Migración ADDITIVE — columna nueva nullable, sin backfill, sin tocar ninguna
existente.

Un catálogo real llegó con 398 productos y **ninguno** con SKU: la identidad
funcionaba igual (``barcode`` → ``sku`` → nombre normalizado, F2/F5), pero el
usuario no tenía ningún código con el cual buscar, etiquetar o referirse a un
producto. El UUID no sirve para eso: nadie escribe
``a7aa9b72-c09d-b88a-beac-4abfb58c24c1`` en una etiqueta.

``internal_sku`` NO reemplaza a ``sku``: son dos cosas distintas y coexisten.
``sku`` es el código que aporta el ARCHIVO o el proveedor —puede faltar, cambiar
o repetirse entre proveedores—; ``internal_sku`` es el de Véktor, estable e
inmutable. Se decidió no renombrar ``sku`` a ``external_sku``: son 415
referencias en 45 archivos de backend y 12 de frontend, y el rename rompe la
compatibilidad hacia atrás durante el deploy (``preDeployCommand`` corre la
migración ANTES de que la versión nueva reciba tráfico, así que la vieja
consultaría una columna que ya no existe).

**Nullable y sin backfill** a propósito: el backfill es una operación aparte.
Consecuencia declarada: los productos que ya existen quedan con
``internal_sku = NULL`` y la pantalla les sigue mostrando "—" hasta que ese
backfill corra. `_ensure_internal_sku` sólo dispara en ``before_insert``, así que
tampoco se los asigna un update. Para la cuenta que motivó el cambio no importa
—se reseteó y va a re-importar desde cero—, pero para cualquier otro tenant el
código aparece recién con los productos nuevos. El índice único es PARCIAL sobre
``internal_sku IS NOT NULL`` justamente para que las filas sin código no
colisionen entre sí.

El nombre del índice queda FUERA de ``_UQ_NAMES``
(``application/services/product_identity.py``) a propósito: esa tabla es la que
decide qué violación de unicidad significa "es el mismo producto, reusalo". Un
``internal_sku`` repetido NO significa eso —son dos productos distintos que
sacaron el mismo número— y tiene que fallar ruidosamente en vez de fusionarlos.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260903_0001"
down_revision = "20260902_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column("internal_sku", sa.String(length=20), nullable=True),
    )
    op.create_index(
        "uq_products_tenant_internal_sku",
        "products",
        ["tenant_id", "internal_sku"],
        unique=True,
        postgresql_where=sa.text("internal_sku IS NOT NULL"),
        sqlite_where=sa.text("internal_sku IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_products_tenant_internal_sku", table_name="products")
    op.drop_column("products", "internal_sku")
