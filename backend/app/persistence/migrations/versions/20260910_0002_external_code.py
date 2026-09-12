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

Por qué el parcial excluye las bajas
-------------------------------------
El predicado no es sólo ``external_code_key IS NOT NULL``: además exige que la
entidad esté viva. La razón es operativa, no estética — **el índice tiene que ver
lo mismo que la búsqueda**. La resolución de identidad se consulta sobre entidades
vivas (``list_for_dedup`` en maestros, ``is_active`` en productos), así que un
índice total dejaría que una entidad dada de baja —invisible para esa búsqueda—
hiciera fallar el insert con un ``IntegrityError`` que nadie puede explicar
mirando los datos activos.

Es el mismo predicado que ``uq_products_tenant_barcode_norm`` y trae su misma
consecuencia conocida: el código de una baja se puede reciclar. Se prefiere eso a
un modo de falla indiagnosticable.

``external_code_key`` se persiste ya normalizada en vez de calcularse en el
predicado, por la misma razón que ``sku_normalized``: el índice tiene que evaluar
exactamente lo mismo que la búsqueda, y dos definiciones de la misma
normalización quedan libres de divergir.

Idempotente (E8c)
-----------------
``upgrade()`` saltea lo que ya existe y ``downgrade()`` sólo borra lo que está.
El ``preDeployCommand`` de Railway corre ``alembic upgrade head`` en cada deploy,
y un esquema que quedó por delante de ``alembic_version`` —pasó el 2026-09-12—
hace fallar el deploy entero con ``DuplicateColumn``. Misma convención que
``20260806_0001``, que ya lo documenta: "el ``preDeployCommand`` puede correr dos
veces".

Límite declarado: comprueba PRESENCIA, no forma. Una columna que exista con otro
tipo se saltea igual; detectar eso pide comparar el esquema entero y es otro
problema.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260910_0002"
down_revision = "20260910_0001"
branch_labels = None
depends_on = None

_TABLAS = ("customers", "suppliers", "products")

#: El predicado de cada tabla es el ESPEJO EXACTO del `Index(...)` del ORM. Si
#: divergen, el esquema que crea la suite (desde el ORM) deja de ser el que corre
#: en producción (desde esta migración), y el modo de falla aparece sólo en prod.
_PREDICADO = {
    "customers": "deactivated_at IS NULL AND external_code_key IS NOT NULL",
    "suppliers": "deactivated_at IS NULL AND external_code_key IS NOT NULL",
    "products": "is_active AND external_code_key IS NOT NULL",
}


def _columnas(tabla: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(tabla)}


def _indices(tabla: str) -> set[str]:
    return {ix["name"] for ix in sa.inspect(op.get_bind()).get_indexes(tabla)}


def upgrade() -> None:
    for tabla in _TABLAS:
        # Columna por columna y no "si falta la primera, agrego las tres": el
        # índice necesita `external_code_key`, así que saltear en bloque por una
        # columna presente dejaría el índice sin crear.
        existentes = _columnas(tabla)
        if "external_code" not in existentes:
            op.add_column(tabla, sa.Column("external_code", sa.String(100), nullable=True))
        if "external_source" not in existentes:
            op.add_column(tabla, sa.Column("external_source", sa.String(60), nullable=True))
        if "external_code_key" not in existentes:
            op.add_column(
                tabla, sa.Column("external_code_key", sa.String(200), nullable=True)
            )
        if f"uq_{tabla}_tenant_external_code" not in _indices(tabla):
            op.create_index(
                f"uq_{tabla}_tenant_external_code",
                tabla,
                ["tenant_id", "external_code_key"],
                unique=True,
                postgresql_where=sa.text(_PREDICADO[tabla]),
                sqlite_where=sa.text(_PREDICADO[tabla]),
            )


def downgrade() -> None:
    for tabla in _TABLAS:
        if f"uq_{tabla}_tenant_external_code" in _indices(tabla):
            op.drop_index(f"uq_{tabla}_tenant_external_code", table_name=tabla)
        existentes = _columnas(tabla)
        for columna in ("external_code_key", "external_source", "external_code"):
            if columna in existentes:
                op.drop_column(tabla, columna)
