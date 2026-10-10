"""Contrato del costo de compra en ``inventory_movements`` (M·1a).

Revision ID: 20261010_0001
Revises: 20261009_0001

Una compra no tiene tabla propia: vive en un ``inventory_movements``
``movement_type='purchase'`` (fecha de negocio, anulación y procedencia ya están
ahí). Se le agregan las columnas del contrato del costo, TODAS nullable: NULL
significa «no se sabe / no se confirmó». Los movimientos viejos quedan en NULL y
no se marcan confirmados retroactivamente.

Más un índice parcial para elegir la última compra viva de un producto.

Aditiva e idempotente (convención E8c; el lock de ``migrations/env.py``
serializa a la API y al worker). Comprueba PRESENCIA, no forma. Los CHECK van
en Postgres con el patrón zero-lock de ``20260804_0001``: ``NOT VALID`` no
escanea y ``VALIDATE`` sólo toma SHARE UPDATE EXCLUSIVE. SQLite ignora los
CHECK de todas formas.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261010_0001"
down_revision = "20261009_0001"
branch_labels = None
depends_on = None

_TABLA = "inventory_movements"
_INDICE = "ix_inventory_movements_live_purchases"

_TRATAMIENTOS = "('incluido','adicional','no_aplica','desconocido')"

_COLUMNAS: tuple[tuple[str, sa.types.TypeEngine[object]], ...] = (
    ("unit_cost_final", sa.Numeric(12, 2)),
    ("shipping_treatment", sa.String(20)),
    ("tax_treatment", sa.String(20)),
    ("tax_rate", sa.Numeric(6, 4)),
    ("tax_amount", sa.Numeric(12, 2)),
    ("tax_recoverable", sa.Boolean()),
    ("document_ref", sa.String(100)),
    ("cost_contract_version", sa.SmallInteger()),
)

# Con NULL, ``x IN (...)`` y ``x >= 0`` dan NULL y el CHECK pasa: es lo buscado
# (NULL = desconocido), así que no hace falta ``IS NULL OR``.
_CHECKS: tuple[tuple[str, str], ...] = (
    ("ck_inventory_movements_shipping_treatment", f"shipping_treatment IN {_TRATAMIENTOS}"),
    ("ck_inventory_movements_tax_treatment", f"tax_treatment IN {_TRATAMIENTOS}"),
    ("ck_inventory_movements_tax_rate", "tax_rate >= 0 AND tax_rate < 1"),
    ("ck_inventory_movements_tax_amount", "tax_amount >= 0"),
    ("ck_inventory_movements_unit_cost_final", "unit_cost_final >= 0"),
    ("ck_inventory_movements_cost_contract_version", "cost_contract_version >= 1"),
)


def _insp() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def upgrade() -> None:
    bind = op.get_bind()
    existentes = {c["name"] for c in _insp().get_columns(_TABLA)}
    for nombre, tipo in _COLUMNAS:
        if nombre not in existentes:
            op.add_column(_TABLA, sa.Column(nombre, tipo, nullable=True))

    if bind.dialect.name == "postgresql":
        presentes = {
            r[0]
            for r in bind.execute(
                sa.text(
                    "SELECT conname FROM pg_constraint"
                    " WHERE conrelid = CAST(:t AS regclass) AND contype = 'c'"
                ),
                {"t": _TABLA},
            )
        }
        for nombre, expr in _CHECKS:
            if nombre not in presentes:
                bind.execute(
                    sa.text(
                        f"ALTER TABLE {_TABLA} ADD CONSTRAINT {nombre} CHECK ({expr}) NOT VALID"
                    )
                )
            bind.execute(sa.text(f"ALTER TABLE {_TABLA} VALIDATE CONSTRAINT {nombre}"))

    indices = {ix["name"] for ix in _insp().get_indexes(_TABLA)}
    if _INDICE not in indices:
        op.create_index(
            _INDICE,
            _TABLA,
            ["tenant_id", "product_id"],
            postgresql_where=sa.text("movement_type = 'purchase' AND voided_at IS NULL"),
            sqlite_where=sa.text("movement_type = 'purchase' AND voided_at IS NULL"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    indices = {ix["name"] for ix in _insp().get_indexes(_TABLA)}
    if _INDICE in indices:
        op.drop_index(_INDICE, table_name=_TABLA)
    if bind.dialect.name == "postgresql":
        for nombre, _ in _CHECKS:
            bind.execute(sa.text(f"ALTER TABLE {_TABLA} DROP CONSTRAINT IF EXISTS {nombre}"))
    existentes = {c["name"] for c in _insp().get_columns(_TABLA)}
    for nombre, _ in reversed(_COLUMNAS):
        if nombre in existentes:
            op.drop_column(_TABLA, nombre)
