"""Amplía CHECK de data_repair_items.action con VOID_DUPLICATE + RECLASSIFY_EXPENSE

Revision ID: 20260717_0002
Revises: 20260717_0001
Create Date: 2026-07-17

Contexto
--------
Migración ADDITIVE (B2 — correctivo de dedup + reclasificación). Los dos nuevos
detectores de DataRepairService auditan sus acciones en `data_repair_items`:

  - DUPLICADOS → `VOID_DUPLICATE` (soft delete de la fila repetida);
  - MISCLASIFICACIÓN OPEX→COGS → `RECLASSIFY_EXPENSE` (cambia category/expense_type).

El CHECK actual de `action` no los contempla. Drop + recreate del constraint con
el catálogo ampliado (reversible: el downgrade vuelve al set previo).
"""

from __future__ import annotations

from alembic import op

revision = "20260717_0002"
down_revision = "20260717_0001"
branch_labels = None
depends_on = None

_OLD_ACTIONS = "'VOID_SALE','CREATE_PRODUCT','UPDATE_PRODUCT','UPDATE_SALE','REVIEW_SALE'"
_NEW_ACTIONS = (
    "'VOID_SALE','CREATE_PRODUCT','UPDATE_PRODUCT','UPDATE_SALE','REVIEW_SALE',"
    "'VOID_DUPLICATE','RECLASSIFY_EXPENSE'"
)


def upgrade() -> None:
    op.drop_constraint("ck_repair_items_action", "data_repair_items", type_="check")
    op.create_check_constraint(
        "ck_repair_items_action",
        "data_repair_items",
        f"action IN ({_NEW_ACTIONS})",
    )


def downgrade() -> None:
    op.drop_constraint("ck_repair_items_action", "data_repair_items", type_="check")
    op.create_check_constraint(
        "ck_repair_items_action",
        "data_repair_items",
        f"action IN ({_OLD_ACTIONS})",
    )
