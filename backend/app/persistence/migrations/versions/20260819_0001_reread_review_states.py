"""Estados de revisión de relectura + updated_at en data_repair_runs

Revision ID: 20260819_0001
Revises: 20260813_0001
Create Date: 2026-08-19

Contexto
--------
F-RR (revisión de relectura, ver reread_service.py): "Volver a leer" pasa a
mostrar el mismo panel de revisión de mapeo que la carga inicial, en vez de
solo contadores agregados. Necesita distinguir server-side las fases previas
al apply (antes todas indistinguibles de RUNNING):

  PREVIEWING     — se re-parseó el archivo, el usuario está revisando/corrigiendo.
  NEEDS_REVIEW   — hay columnas ambiguas/riesgosas sin decisión del usuario.
  READY_TO_APPLY — el usuario terminó de revisar, sin bloqueos.
  QUEUED         — se encoló el apply, ningún worker lo tomó todavía.
  APPLYING       — un worker lo tomó y está corriendo la reconciliación.

APPLIED/FAILED ya existían y se reusan tal cual. Migración ADDITIVE:

  1. Amplía el CHECK ``ck_repair_runs_status`` (drop + recreate, reversible).
  2. Agrega ``data_repair_runs.updated_at`` (NOT NULL, default `now()` para
     las filas existentes) — el sweep de sesiones/jobs huérfanos lo necesita
     para saber cuánto hace que un run no avanzó; `created_at` no alcanza
     porque una sesión de revisión legítima puede estar abierta mucho más
     que el umbral de "colgado" mientras el usuario corrige mapeos.

No reescribe filas de negocio, no borra datos.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260819_0001"
down_revision = "20260813_0001"
branch_labels = None
depends_on = None


_STATUS_OLD = (
    "'PENDING','RUNNING','COMPLETED','FAILED','APPROVED','APPLIED',"
    "'REVERTED','PARTIALLY_APPLIED','COMPLETED_WITH_ERRORS'"
)
_STATUS_NEW = (
    _STATUS_OLD + ",'PREVIEWING','NEEDS_REVIEW','READY_TO_APPLY','QUEUED','APPLYING'"
)


def upgrade() -> None:
    op.add_column(
        "data_repair_runs",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.drop_constraint("ck_repair_runs_status", "data_repair_runs", type_="check")
    op.create_check_constraint(
        "ck_repair_runs_status", "data_repair_runs", f"status IN ({_STATUS_NEW})"
    )


def downgrade() -> None:
    op.drop_constraint("ck_repair_runs_status", "data_repair_runs", type_="check")
    op.create_check_constraint(
        "ck_repair_runs_status", "data_repair_runs", f"status IN ({_STATUS_OLD})"
    )
    op.drop_column("data_repair_runs", "updated_at")
