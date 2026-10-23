"""Ampliar CHECK de entity_type en tenant_column_mappings a customer|supplier

Revision ID: 20260803_0001
Revises: 20260802_0001
Create Date: 2026-08-03

Contexto
--------
F7a (pipeline universal de ingesta) agrega ``customer`` y ``supplier`` como
entity_type reconocidos en toda la cadena de detección/mapeo de columnas. El
CHECK ``ck_tcm_entity_type`` de ``tenant_column_mappings`` (creado en
``20260620_0001``) seguía restringido a ``('sale','expense','product',
'inventory')`` — un INSERT con ``entity_type='customer'`` o ``'supplier'``
fallaría con IntegrityError en Postgres (500) apenas 7b/7c empiecen a guardar
mapeos aprendidos para esas entidades.

Esta migración recrea el CHECK con la lista ampliada, mismo patrón que
``20260722_0002_widen_field_entity_type_check.py`` (que hizo lo propio para
``vertical_field_definitions``/``tenant_custom_field_definitions``). ADDITIVE:
solo amplía el dominio permitido, no reescribe filas. ``downgrade`` restaura
el dominio original (seguro: ninguna fila previa usaba los valores nuevos).

Solo aplica a Postgres (los tests usan SQLite, que ignora los CHECK — el
harness de tests además arma el schema vía ``Base.metadata.create_all``, sin
correr Alembic).
"""

from __future__ import annotations

from alembic import op

revision = "20260803_0001"
down_revision = "20260802_0001"
branch_labels = None
depends_on = None

_NEW_VALUES = "('sale', 'expense', 'product', 'inventory', 'customer', 'supplier')"
_OLD_VALUES = "('sale', 'expense', 'product', 'inventory')"

_TABLE = "tenant_column_mappings"
_CONSTRAINT = "ck_tcm_entity_type"


def _recreate(values: str) -> None:
    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_CONSTRAINT}")
    op.execute(f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_CONSTRAINT} CHECK (entity_type IN {values})")


def upgrade() -> None:
    _recreate(_NEW_VALUES)


def downgrade() -> None:
    _recreate(_OLD_VALUES)
