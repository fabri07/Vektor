"""``has_user_edits`` en clientes y proveedores (E6a) — el import deja de pisar

Revision ID: 20260910_0003
Revises: 20260910_0002
Create Date: 2026-09-10

Contexto
--------
Migración ADDITIVE: una columna booleana NOT NULL con ``server_default='false'``
en ``customers`` y ``suppliers``. El default del servidor hace que las filas
existentes queden en ``false`` sin backfill ni bloqueo de tabla.

Por qué hace falta AHORA y no antes
------------------------------------
La política de actualización del import de maestros es "actualizar los campos
provistos, sin pisar con vacío". Eso alcanzaba mientras la única forma de
matchear un maestro existente fuera un documento o un email — pocas filas
matcheaban, y las que lo hacían venían de la misma fuente que las había creado.

El código externo (``20260910_0002``) cambia esa aritmética: es la clave que
existe para el maestro de un kiosco, así que **muchas más** filas van a matchear
contra fichas que el usuario ya tocó a mano. Sin esta columna, habilitar la clave
fuerte significa habilitar que cada re-importación pise las correcciones del
usuario en silencio.

Es la contracara de la separación entre identidad y actualización: que dos filas
sean la MISMA entidad —lo que decide la clave fuerte— no dice nada sobre cuál de
las dos versiones de un teléfono vale. Lo primero lo resuelve la identidad; lo
segundo es una decisión que la marca hace explícita.

Granularidad
------------
A nivel entidad, igual que ``sales_entries.has_user_edits``, y no por campo. Se
banca porque la política que habilita es ADITIVA, no un bloqueo: el import sigue
pudiendo completar los campos que el usuario nunca cargó, y sólo pierde el derecho
a sobrescribir los que sí.

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

revision = "20260910_0003"
down_revision = "20260910_0002"
branch_labels = None
depends_on = None

_TABLAS = ("customers", "suppliers")


def _columnas(tabla: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(tabla)}


def upgrade() -> None:
    for tabla in _TABLAS:
        if "has_user_edits" in _columnas(tabla):
            continue
        op.add_column(
            tabla,
            sa.Column(
                "has_user_edits",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )


def downgrade() -> None:
    for tabla in _TABLAS:
        if "has_user_edits" in _columnas(tabla):
            op.drop_column(tabla, "has_user_edits")
