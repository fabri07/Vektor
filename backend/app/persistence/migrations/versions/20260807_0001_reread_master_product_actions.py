"""Acciones de auditoría para undo de maestros (clientes/proveedores) en relecturas

Revision ID: 20260807_0001
Revises: 20260806_0003
Create Date: 2026-07-29

Contexto
--------
Migración ADDITIVE — F9b: el undo de una relectura hoy no puede revertir
clientes/proveedores tocados (sin rastro before/after) ni productos creados por
ella (sin motivo de baja dedicado). Amplía dos CHECK constraints:

  1. ``data_repair_items.action`` → agrega ``'REREAD_MASTER_CREATE'`` +
     ``'REREAD_MASTER_UPDATE'`` (auditoría before/after de Customer/Supplier
     tocados por una relectura, para poder revertirlos en el undo).
  2. ``products.deactivation_reason`` → agrega ``'REREAD_UNDO'`` (soft-delete de
     un producto creado por una relectura que luego se deshace — nunca hard
     delete, evita romper referencias de ventas/gastos históricos).

Todo es drop + recreate de constraints (reversible: el downgrade restaura el
set previo). No reescribe filas.
"""

from __future__ import annotations

from alembic import op

revision = "20260807_0001"
down_revision = "20260806_0003"
branch_labels = None
depends_on = None


_ACTION_OLD = (
    "'VOID_SALE','CREATE_PRODUCT','UPDATE_PRODUCT','UPDATE_SALE','REVIEW_SALE',"
    "'VOID_DUPLICATE','RECLASSIFY_EXPENSE','REREAD_VOID','REREAD_INSERT',"
    "'MERGE_PRODUCT','DEACTIVATE_DUPLICATE','REPOINT_FK','CONSOLIDATE_BALANCE',"
    "'DELETE_BALANCE'"
)
_ACTION_NEW = _ACTION_OLD + ",'REREAD_MASTER_CREATE','REREAD_MASTER_UPDATE'"

_PRODUCT_DEACT_OLD = "'USER_CANCELLED','DUPLICATE','MANUAL_ADMIN_VOID'"
_PRODUCT_DEACT_NEW = _PRODUCT_DEACT_OLD + ",'REREAD_UNDO'"


def _rebuild(table: str, name: str, expr: str) -> None:
    op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(name, table, expr)


def upgrade() -> None:
    _rebuild("data_repair_items", "ck_repair_items_action", f"action IN ({_ACTION_NEW})")
    _rebuild(
        "products",
        "ck_products_deactivation_reason",
        f"deactivation_reason IS NULL OR deactivation_reason IN ({_PRODUCT_DEACT_NEW})",
    )


def downgrade() -> None:
    _rebuild("data_repair_items", "ck_repair_items_action", f"action IN ({_ACTION_OLD})")
    _rebuild(
        "products",
        "ck_products_deactivation_reason",
        f"deactivation_reason IS NULL OR deactivation_reason IN ({_PRODUCT_DEACT_OLD})",
    )
