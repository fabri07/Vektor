"""Amplía CHECK constraints de repair para dedup auditado de productos (Fase 3)

Revision ID: 20260731_0003
Revises: 20260731_0002
Create Date: 2026-07-31

Contexto
--------
Migración ADDITIVE — base mecánica de la Fase 3 (dedup auditado de productos:
mergear duplicados de identidad detectados en Fase 2). Amplía dos CHECK
constraints sin tocar datos existentes:

  1. ``data_repair_items.action`` → agrega ``'MERGE_PRODUCT'``,
     ``'DEACTIVATE_DUPLICATE'``, ``'REPOINT_FK'``, ``'CONSOLIDATE_BALANCE'``,
     ``'DELETE_BALANCE'`` (auditoría de cada paso del merge de un producto
     duplicado: reasignar FKs de ventas/gastos/movimientos, consolidar o
     borrar el balance de inventario redundante).
  2. ``data_repair_runs.status`` → agrega ``'PARTIALLY_APPLIED'`` y
     ``'COMPLETED_WITH_ERRORS'`` (un run de dedup puede aplicar algunos
     merges y fallar en otros — no es todo-o-nada como los repairs previos).

Todo es drop + recreate de constraints (reversible: el downgrade restaura el
set previo). No reescribe filas.
"""

from __future__ import annotations

from alembic import op

revision = "20260731_0003"
down_revision = "20260731_0002"
branch_labels = None
depends_on = None


# ── data_repair_items.action ───────────────────────────────────────────────────
_ACTION_OLD = (
    "'VOID_SALE','CREATE_PRODUCT','UPDATE_PRODUCT','UPDATE_SALE','REVIEW_SALE',"
    "'VOID_DUPLICATE','RECLASSIFY_EXPENSE','REREAD_VOID','REREAD_INSERT'"
)
_ACTION_NEW = (
    "'VOID_SALE','CREATE_PRODUCT','UPDATE_PRODUCT','UPDATE_SALE','REVIEW_SALE',"
    "'VOID_DUPLICATE','RECLASSIFY_EXPENSE','REREAD_VOID','REREAD_INSERT',"
    "'MERGE_PRODUCT','DEACTIVATE_DUPLICATE','REPOINT_FK','CONSOLIDATE_BALANCE',"
    "'DELETE_BALANCE'"
)

# ── data_repair_runs.status ────────────────────────────────────────────────────
_STATUS_OLD = "'PENDING','RUNNING','COMPLETED','FAILED','APPROVED','APPLIED','REVERTED'"
_STATUS_NEW = (
    "'PENDING','RUNNING','COMPLETED','FAILED','APPROVED','APPLIED','REVERTED',"
    "'PARTIALLY_APPLIED','COMPLETED_WITH_ERRORS'"
)


def _rebuild(table: str, name: str, expr: str) -> None:
    op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(name, table, expr)


def upgrade() -> None:
    _rebuild(
        "data_repair_items",
        "ck_repair_items_action",
        f"action IN ({_ACTION_NEW})",
    )
    _rebuild(
        "data_repair_runs",
        "ck_repair_runs_status",
        f"status IN ({_STATUS_NEW})",
    )


def downgrade() -> None:
    _rebuild(
        "data_repair_items",
        "ck_repair_items_action",
        f"action IN ({_ACTION_OLD})",
    )
    _rebuild(
        "data_repair_runs",
        "ck_repair_runs_status",
        f"status IN ({_STATUS_OLD})",
    )
