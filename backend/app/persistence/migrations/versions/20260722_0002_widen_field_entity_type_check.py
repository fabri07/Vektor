"""Ampliar CHECK de entity_type en field-definitions a customer|supplier|marketing

Revision ID: 20260722_0002
Revises: 20260722_0001
Create Date: 2026-07-22

Contexto
--------
La reforma de columnas personalizadas habilitó crear field-definitions para las
secciones Clientes, Proveedores y Marketing (además de las originales
Ventas/Gastos/Productos/Inventario). El schema Pydantic ya acepta esos
``entity_type``, pero los CHECK de la DB creados en ``20260508_0001``
(``ck_vfd_entity_type`` en ``vertical_field_definitions`` y
``ck_tcfd_entity_type`` en ``tenant_custom_field_definitions``) seguían
restringidos a ``('sale','expense','product','inventory')`` — un INSERT con
``entity_type='customer'`` fallaba con IntegrityError en Postgres (500).

Esta migración recrea ambos CHECK con la lista ampliada. ADDITIVE: solo amplía
el dominio permitido, no reescribe filas. ``downgrade`` restaura el dominio
original (seguro: ninguna fila previa usaba los valores nuevos).

Solo aplica a Postgres (los tests usan SQLite, que ignora los CHECK).
"""

from __future__ import annotations

from alembic import op

revision = "20260722_0002"
down_revision = "20260722_0001"
branch_labels = None
depends_on = None

_NEW_VALUES = "('sale', 'expense', 'product', 'inventory', 'customer', 'supplier', 'marketing')"
_OLD_VALUES = "('sale', 'expense', 'product', 'inventory')"

_TARGETS = (
    ("vertical_field_definitions", "ck_vfd_entity_type"),
    ("tenant_custom_field_definitions", "ck_tcfd_entity_type"),
)


def _recreate(values: str) -> None:
    for table, constraint in _TARGETS:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {constraint}")
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {constraint} " f"CHECK (entity_type IN {values})"
        )


def upgrade() -> None:
    _recreate(_NEW_VALUES)


def downgrade() -> None:
    _recreate(_OLD_VALUES)
