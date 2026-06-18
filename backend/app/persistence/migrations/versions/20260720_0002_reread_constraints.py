"""Constraints aditivas para la relectura de archivos (REREAD_FILE)

Revision ID: 20260720_0002
Revises: 20260720_0001
Create Date: 2026-07-20

Contexto
--------
Migración ADDITIVE — habilita la "relectura de archivos" (re-importa un archivo
ya subido preservando ediciones manuales, auditado y reversible). Amplía tres
CHECK constraints sin tocar datos existentes:

  1. ``sales_entries.void_reason`` / ``expense_entries.void_reason`` →
     agrega ``'REREAD_REIMPORT'`` (soft delete de un registro no-editado que la
     relectura re-importa corregido).
  2. ``data_repair_items.action`` → agrega ``'REREAD_VOID'`` + ``'REREAD_INSERT'``
     (auditoría de cada void/insert de la relectura, para before/after y undo).
  3. ``data_repair_runs.status`` → agrega ``'REVERTED'`` (estado de un run de
     relectura deshecho con ``undo_reread``).

Todo es drop + recreate de constraints (reversible: el downgrade restaura el set
previo). No reescribe filas.
"""

from __future__ import annotations

from alembic import op

revision = "20260720_0002"
down_revision = "20260720_0001"
branch_labels = None
depends_on = None


# ── void_reason (sales_entries / expense_entries) ──────────────────────────────
_VOID_OLD = "'REPAIR_MISCLASSIFIED_IMPORT','USER_CANCELLED','DUPLICATE','MANUAL_ADMIN_VOID'"
_VOID_NEW = (
    "'REPAIR_MISCLASSIFIED_IMPORT','USER_CANCELLED','DUPLICATE','MANUAL_ADMIN_VOID',"
    "'REREAD_REIMPORT'"
)

# ── data_repair_items.action ───────────────────────────────────────────────────
_ACTION_OLD = (
    "'VOID_SALE','CREATE_PRODUCT','UPDATE_PRODUCT','UPDATE_SALE','REVIEW_SALE',"
    "'VOID_DUPLICATE','RECLASSIFY_EXPENSE'"
)
_ACTION_NEW = (
    "'VOID_SALE','CREATE_PRODUCT','UPDATE_PRODUCT','UPDATE_SALE','REVIEW_SALE',"
    "'VOID_DUPLICATE','RECLASSIFY_EXPENSE','REREAD_VOID','REREAD_INSERT'"
)

# ── data_repair_runs.status ────────────────────────────────────────────────────
_STATUS_OLD = "'PENDING','RUNNING','COMPLETED','FAILED','APPROVED','APPLIED'"
_STATUS_NEW = "'PENDING','RUNNING','COMPLETED','FAILED','APPROVED','APPLIED','REVERTED'"


def _rebuild(table: str, name: str, expr: str) -> None:
    op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(name, table, expr)


def upgrade() -> None:
    _rebuild(
        "sales_entries",
        "ck_sales_entries_void_reason",
        f"void_reason IS NULL OR void_reason IN ({_VOID_NEW})",
    )
    _rebuild(
        "expense_entries",
        "ck_expense_entries_void_reason",
        f"void_reason IS NULL OR void_reason IN ({_VOID_NEW})",
    )
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
        "sales_entries",
        "ck_sales_entries_void_reason",
        f"void_reason IS NULL OR void_reason IN ({_VOID_OLD})",
    )
    _rebuild(
        "expense_entries",
        "ck_expense_entries_void_reason",
        f"void_reason IS NULL OR void_reason IN ({_VOID_OLD})",
    )
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
