"""Framework de versionado de ingestión (F9a)

Revision ID: 20260805_0001
Revises: 20260804_0001
Create Date: 2026-08-05

Contexto
--------
Migración ADDITIVE que agrega cinco columnas nuevas a ``uploaded_files`` para
soportar versionado de la lógica de interpretación de ingestión (qué protocolo
se usó: pre-F8 vs F8 con riesgo contextual de columnas, etc.) y reprocesamiento
(reread) multipass del archivo sin resubir.

**Columnas nuevas:**

1. ``ingestion_version``: INT NOT NULL DEFAULT 1
   - Qué versión del pipeline interpretó este archivo.
   - 1 = baseline pre-F8 (ninguno tenía riesgo de columnas).
   - 2 = F8+ (protocolo F8 de riesgo contextual ya pasó).
   - Permite evolucionar la lógica sin perder trazabilidad.

2. ``latest_preview_version``: INT NULLABLE
   - La versión más reciente de preview mostrada. Separada de
     ``ingestion_version`` para distinguir "reread y visto" de "procesado".

3. ``reread_status``: VARCHAR(30) NOT NULL DEFAULT 'NONE'
   - Estado del reprocesamiento sin resubir el archivo.
   - Valores: NONE, PREVIEWED, UP_TO_DATE, NEEDS_REVIEW, AUTO_APPLIED, APPLIED, FAILED.
   - Incluye CHECK constraint.

4. ``reread_at``: DATETIME(timezone=True) NULLABLE
   - Cuándo se hizo el reprocesamiento más reciente (reloj de PG).

5. ``reread_summary``: JSONB NULLABLE
   - Summary del reread (diagnóstico, cambios detectados, etc.); formato
     flexionado por la versión del protocolo que lo generó.

**UPDATE de evidencia (Fase de populado):**

``ingestion_version = 2`` con evidencia únicamente para archivos ya confirmados
(``processing_status = 'DONE'``) que tienen la key ``column_risk_decisions`` en
el summary. Esta key solo la crea F8 (si hay decisiones efectivas que tomar), así
que su presencia prueba inequívocamente que el archivo ya pasó por F8+. Todo
lo demás (sin la key) queda conservadoramente en 1.

Razón: un archivo confirmado PRE-F8 y uno confirmado POST-F8 sin columnas
riesgosas tienen el mismo summary (ninguno tiene la key) — no se puede distinguir
"ya pasó por F8 y no había nada" de "nunca pasó por F8" salvo por evidencia en
los datos. La presencia de la key es evidencia inequívoca.

``downgrade``: revertir las columnas en orden inverso.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260805_0001"
down_revision = "20260804_0001"
branch_labels = None
depends_on = None

# Constantes de módulo para permitir monkeypatch en tests PG-gated.
TABLE_NAME = "uploaded_files"
CONSTRAINT_NAME = "ck_uploaded_files_reread_status"


def upgrade() -> None:
    # Agregar las columnas nuevas (additive puro, con defaults).
    op.add_column(
        TABLE_NAME,
        sa.Column("ingestion_version", sa.Integer, nullable=False, server_default="1"),
    )
    op.add_column(
        TABLE_NAME,
        sa.Column("latest_preview_version", sa.Integer, nullable=True),
    )
    op.add_column(
        TABLE_NAME,
        sa.Column("reread_status", sa.String(30), nullable=False, server_default="NONE"),
    )
    op.add_column(
        TABLE_NAME,
        sa.Column("reread_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        TABLE_NAME,
        sa.Column("reread_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )

    # CHECK constraint para reread_status (valores válidos).
    op.create_check_constraint(
        CONSTRAINT_NAME,
        TABLE_NAME,
        "reread_status IN ('NONE','PREVIEWED','UP_TO_DATE','NEEDS_REVIEW',"
        "'AUTO_APPLIED','APPLIED','FAILED')",
    )

    # UPDATE de evidencia: marcar archivos ya confirmados con ``column_risk_decisions``
    # como ingestion_version=2 (F8+). La presencia de esta key es evidencia
    # inequívoca de que pasó por F8+.
    op.execute(
        sa.text(
            f"UPDATE {TABLE_NAME} SET ingestion_version = 2 "
            "WHERE processing_status = 'DONE' AND parsed_summary_json ? 'column_risk_decisions'"
        )
    )


def downgrade() -> None:
    # Revertir en orden inverso de creación.
    op.drop_constraint(CONSTRAINT_NAME, TABLE_NAME, type_="check")
    op.drop_column(TABLE_NAME, "reread_summary")
    op.drop_column(TABLE_NAME, "reread_at")
    op.drop_column(TABLE_NAME, "reread_status")
    op.drop_column(TABLE_NAME, "latest_preview_version")
    op.drop_column(TABLE_NAME, "ingestion_version")
