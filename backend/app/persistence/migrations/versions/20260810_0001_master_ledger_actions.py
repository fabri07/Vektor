"""Acciones de ledger para clientes y proveedores creados/modificados por un import.

Hasta acá el ledger de reversa solo registraba PRODUCTOS
(``CREATE_PRODUCT``/``UPDATE_PRODUCT``), así que borrar un archivo no podía
revertir los clientes ni los proveedores que ese archivo había traído: quedaban
vivos, sin manera de saber de dónde salieron.

Las cuatro acciones nuevas son paralelas a las de producto a propósito. Las de
relectura (``REREAD_MASTER_CREATE``/``REREAD_MASTER_UPDATE``) se conservan —
tienen datos vivos y son de otro camino—, pero el import no las reusa: mezclar
las dos convenciones en el mismo CHECK obligaría a adivinar cuál mira cada
consulta.

Additive y sin backfill: los imports anteriores no registraron maestros y no hay
forma de reconstruir qué crearon. El borrado los informa como no revertibles en
vez de adivinar.
"""

from __future__ import annotations

from alembic import op

revision = "20260810_0001"
down_revision = "20260809_0001"
branch_labels = None
depends_on = None

_TABLE = "data_repair_items"
_CONSTRAINT = "ck_repair_items_action"

_ACCIONES_PREVIAS = (
    "VOID_SALE",
    "CREATE_PRODUCT",
    "UPDATE_PRODUCT",
    "UPDATE_SALE",
    "REVIEW_SALE",
    "VOID_DUPLICATE",
    "RECLASSIFY_EXPENSE",
    "REREAD_VOID",
    "REREAD_INSERT",
    "MERGE_PRODUCT",
    "DEACTIVATE_DUPLICATE",
    "REPOINT_FK",
    "CONSOLIDATE_BALANCE",
    "DELETE_BALANCE",
    "REREAD_MASTER_CREATE",
    "REREAD_MASTER_UPDATE",
)

_ACCIONES_NUEVAS = (
    "CREATE_CUSTOMER",
    "UPDATE_CUSTOMER",
    "CREATE_SUPPLIER",
    "UPDATE_SUPPLIER",
)


def _check(acciones: tuple[str, ...]) -> str:
    valores = ",".join(f"'{a}'" for a in acciones)
    return f"action IN ({valores})"


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(
        _CONSTRAINT, _TABLE, _check(_ACCIONES_PREVIAS + _ACCIONES_NUEVAS)
    )


def downgrade() -> None:
    # Las filas con las acciones nuevas violarían el CHECK viejo: se borran antes.
    # Es seguro — son items de ledger de imports que, al bajar esta migración,
    # el código tampoco sabría interpretar.
    valores = ",".join(f"'{a}'" for a in _ACCIONES_NUEVAS)
    op.execute(f"DELETE FROM {_TABLE} WHERE action IN ({valores})")  # noqa: S608
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_CONSTRAINT, _TABLE, _check(_ACCIONES_PREVIAS))
