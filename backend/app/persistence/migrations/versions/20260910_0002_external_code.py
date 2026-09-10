"""Código externo de maestros y productos (E6a) — columnas + único parcial

Revision ID: 20260910_0002
Revises: 20260910_0001
Create Date: 2026-09-10

Contexto
--------
Migración ADDITIVE: tres columnas nullable en ``customers``, ``suppliers`` y
``products``, más un índice ÚNICO PARCIAL por tabla. Ninguna columna existente se
toca y **no hay backfill**.

Por qué el único va desde el principio y no después
---------------------------------------------------
La regla general del programa es prevalidar colisiones antes de imponer un único
—un tenant con códigos repetidos haría fallar la migración—, pero esa regla
protege datos que YA existen. Acá las tres columnas nacen vacías: no hay una sola
fila con código, así que no hay nada que colisionar y el índice no puede fallar.

Y hace falta desde el principio, no "cuando haya datos": el código externo es una
clave FUERTE, o sea que la resolución de identidad va a decidir con él. Un índice
común más un detector no impiden que **dos imports simultáneos creen dos entidades
con el mismo código** — para eso está el UNIQUE, que es lo único que dos
transacciones concurrentes no pueden atravesar. Habilitar la clave sin él sería
prometer una identidad que la base no sostiene.

Si en algún momento hubiera que POBLAR estas columnas desde datos existentes, ahí
sí manda la regla general: diagnosticar colisiones primero y recién después
activar la restricción.

Por qué el parcial NO filtra por baja
--------------------------------------
El predicado es sólo ``external_code_key IS NOT NULL``, igual que
``uq_products_tenant_internal_sku`` y a diferencia de los índices de barcode/sku
(que sí exigen ``is_active``). El código de una entidad dada de baja **no se
recicla**: sigue escrito en los remitos y las facturas viejas que la nombran.
Además, un índice que excluyera las bajas haría fallar la reactivación cuando
otro le hubiera tomado el código mientras tanto.

``external_code_key`` se persiste ya normalizada en vez de calcularse en el
predicado, por la misma razón que ``sku_normalized``: el índice tiene que evaluar
exactamente lo mismo que la búsqueda, y dos definiciones de la misma
normalización quedan libres de divergir.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260910_0002"
down_revision = "20260910_0001"
branch_labels = None
depends_on = None

_TABLAS = ("customers", "suppliers", "products")


def upgrade() -> None:
    for tabla in _TABLAS:
        op.add_column(tabla, sa.Column("external_code", sa.String(100), nullable=True))
        op.add_column(tabla, sa.Column("external_source", sa.String(60), nullable=True))
        op.add_column(tabla, sa.Column("external_code_key", sa.String(200), nullable=True))
        op.create_index(
            f"uq_{tabla}_tenant_external_code",
            tabla,
            ["tenant_id", "external_code_key"],
            unique=True,
            postgresql_where=sa.text("external_code_key IS NOT NULL"),
            sqlite_where=sa.text("external_code_key IS NOT NULL"),
        )


def downgrade() -> None:
    for tabla in _TABLAS:
        op.drop_index(f"uq_{tabla}_tenant_external_code", table_name=tabla)
        op.drop_column(tabla, "external_code_key")
        op.drop_column(tabla, "external_source")
        op.drop_column(tabla, "external_code")
