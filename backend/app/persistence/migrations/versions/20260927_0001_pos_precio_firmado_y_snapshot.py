"""Precio firmado, ticket congelado y atribución al turno (B7·1 del POS).

Revision ID: 20260927_0001
Revises: 20260926_0001

``pos_operations``:

* ``captured_offline`` — la caja cobró y cerró el ticket sin respuesta del
  servidor. No prueba que el efectivo entró; es la afirmación que habilita
  capturar una salida pendiente de stock (B11), atribuida y auditada.
* ``charged_at`` — cuándo el cajero confirmó el pago. Distinto de
  ``operation_date`` (fecha de negocio) y de ``created_at`` (cuándo llegó).
* ``client_session_id`` — el turno de caja del cliente, para que B8 pueda
  atribuir una venta que sincroniza después del arqueo.

``pos_operation_lines``:

* ``unit_price_catalog_ars`` — el precio que tenía el catálogo al sincronizar,
  sólo si difería del que se cobró.
* ``product_name_snapshot`` / ``sale_unit_snapshot`` / ``base_units_snapshot``
  — el ticket deja de releer el catálogo actual. ``String(300)`` porque
  ``products.name`` es ``String(300)``: con 255 se truncaría un nombre válido.

**Sin backfill.** Una línea anterior a B7 lee NULL y el recibo cae al catálogo,
como hasta ahora: no se sabe cómo se llamaba el producto cuando se vendió, y
escribir el nombre de hoy sería justamente la mentira que esto viene a evitar.

Aditiva e idempotente (convención E8c; el lock de ``migrations/env.py``
serializa a la API y al worker). Comprueba PRESENCIA, no forma.
"""

from __future__ import annotations

from collections.abc import Callable

import sqlalchemy as sa

from alembic import op

revision = "20260927_0001"
down_revision = "20260926_0001"
branch_labels = None
depends_on = None

_OPERACIONES = "pos_operations"
_LINEAS = "pos_operation_lines"

#: Fábricas y no instancias: un ``sa.Column`` se consume al agregarlo (queda
#: atado a su tabla), así que reusar el mismo objeto entre upgrade y downgrade
#: —o entre dos corridas— rompe.
_NUEVAS: dict[str, list[tuple[str, Callable[[], sa.Column[object]]]]] = {
    _OPERACIONES: [
        (
            "captured_offline",
            lambda: sa.Column(
                "captured_offline", sa.Boolean(), nullable=False, server_default=sa.false()
            ),
        ),
        ("charged_at", lambda: sa.Column("charged_at", sa.DateTime(), nullable=True)),
        (
            "client_session_id",
            lambda: sa.Column("client_session_id", sa.String(64), nullable=True),
        ),
    ],
    _LINEAS: [
        (
            "unit_price_catalog_ars",
            lambda: sa.Column("unit_price_catalog_ars", sa.Numeric(12, 2), nullable=True),
        ),
        (
            "product_name_snapshot",
            lambda: sa.Column("product_name_snapshot", sa.String(300), nullable=True),
        ),
        (
            "sale_unit_snapshot",
            lambda: sa.Column("sale_unit_snapshot", sa.String(20), nullable=True),
        ),
        (
            "base_units_snapshot",
            lambda: sa.Column("base_units_snapshot", sa.Integer(), nullable=True),
        ),
    ],
}


def _insp() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def upgrade() -> None:
    for tabla, columnas in _NUEVAS.items():
        existentes = {c["name"] for c in _insp().get_columns(tabla)}
        for nombre, fabrica in columnas:
            if nombre not in existentes:
                op.add_column(tabla, fabrica())


def downgrade() -> None:
    for tabla, columnas in _NUEVAS.items():
        existentes = {c["name"] for c in _insp().get_columns(tabla)}
        a_borrar = [nombre for nombre, _ in columnas if nombre in existentes]
        if not a_borrar:
            continue
        if op.get_bind().dialect.name == "sqlite":
            with op.batch_alter_table(tabla) as batch:
                for nombre in a_borrar:
                    batch.drop_column(nombre)
        else:
            for nombre in a_borrar:
                op.drop_column(tabla, nombre)
