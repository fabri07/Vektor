"""CHECK: todo movimiento 'adjustment' debe tener source_type.

Revision ID: 20260728_0001
Revises: 20260727_0001
Create Date: 2026-07-28

Contexto
--------
Migración `20260727_0001` agregó `source_type`/`source_upload_id`/`source_row_ref`/
`source_row_hash` a `inventory_movements`, pero todas nullable — nada obligaba a
completarlas. Un incidente real (tenant "don pedro", 2026-07) mostró que un
`adjustment` sin procedencia rastreable puede quedar en la DB indefinidamente sin
forma de reconciliar si es real o ruido (revert_brand_supplier_collapse /
repair_inventory_ledger no pudieron distinguirlo sin reconstrucción manual contra
archivos fuente del tenant).

Esta migración agrega un CHECK a nivel DB: `voided_at IS NOT NULL OR
movement_type <> 'adjustment' OR source_type IS NOT NULL`. Exime explícitamente los
movimientos ya anulados (`voided_at`): una fila voideada no participa en el cálculo
de stock actual, así que no tiene sentido bloquear el deploy por procedencia de
historia ya descartada — el objetivo es blindar el stock VIVO hacia adelante, no
reescribir el pasado. Alcance deliberadamente limitado a `adjustment` — hoy
`purchase`/`sale`/`loss` (creados en `stock_service.py`) tampoco setean
`source_type`; extender el CHECK a esos tipos requeriría primero que ese servicio
empiece a taggear origen, fuera de alcance de esta migración.

PASO MANUAL PREVIO (no automatizable): correr
`scripts/diag_adjustment_missing_source_type.py` contra Neon antes de mergear esto
(ya excluye voideados). Si devuelve filas VIVAS sin source_type, el CHECK falla al
aplicarse — hay que decidir su destino (anular por ser ruido no reconciliable, o
backfillear `source_type='reconciliation'` si se confirma que son ajustes reales)
antes de mergear. Ver el incidente real de "don pedro" (2026-07): 655 filas vivas
sin source_type, todas de una misma corrida masiva 2026-06-13, con signo negativo
y montos incompatibles con una reconciliación catálogo+compras−ventas para varios
productos — no se asume su naturaleza sin verificar caso por caso.

Idempotente vía `sa.inspect` (mismo motivo que `20260727_0001`: más de un servicio
de Railway puede correr `alembic upgrade head` en paralelo contra la misma DB en un
mismo deploy).

Solo aplica a Postgres (SQLite, usado en tests, ignora los CHECK creados fuera del
DDL de creación de tabla).
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260728_0001"
down_revision = "20260727_0002"
branch_labels = None
depends_on = None

_TABLE = "inventory_movements"
_CONSTRAINT = "ck_inventory_movements_adjustment_source_type"
_EXPR = "voided_at IS NOT NULL OR movement_type <> 'adjustment' OR source_type IS NOT NULL"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {c["name"] for c in inspector.get_check_constraints(_TABLE)}
    if _CONSTRAINT not in existing:
        op.create_check_constraint(_CONSTRAINT, _TABLE, _EXPR)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {c["name"] for c in inspector.get_check_constraints(_TABLE)}
    if _CONSTRAINT in existing:
        op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
