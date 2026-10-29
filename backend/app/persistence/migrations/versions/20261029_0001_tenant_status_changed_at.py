"""Panel SUPERADMIN F2: ``tenants.status_changed_at``.

Cuándo pasó la cuenta a su estado actual. Lo necesita la caja offline: una venta
cobrada ANTES del corte (``charged_at`` < ``status_changed_at``) se acepta al
reconectar aunque la cuenta esté en solo lectura; una posterior se rechaza
(``docs/plans/superadmin-panel.md`` §5). NULL = nunca se cambió desde el panel,
y con la cuenta bloqueada NULL **no habilita** ninguna excepción (falla cerrado).

Aditiva e idempotente (convención E8c; el lock de ``migrations/env.py``
serializa a la API y al worker). Comprueba PRESENCIA, no forma. Sin backfill:
inventar una fecha para tenants ya bloqueados sería declarar un corte que nadie
hizo.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261029_0001"
down_revision = "20261027_0001"
branch_labels = None
depends_on = None

_TABLA = "tenants"
_COLUMNA = "status_changed_at"


def _columnas() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_TABLA)}


def upgrade() -> None:
    if _COLUMNA not in _columnas():
        op.add_column(_TABLA, sa.Column(_COLUMNA, sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    if _COLUMNA in _columnas():
        op.drop_column(_TABLA, _COLUMNA)
