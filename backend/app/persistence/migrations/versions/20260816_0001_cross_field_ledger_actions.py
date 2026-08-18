"""Acciones de ledger para campos cross-sección (venta→cliente, compra→proveedor).

F-D captura un campo cross (ej. `customer:last_name` mapeado en una hoja de
ventas) al buffer y lo escribe UNA vez por entidad resuelta. Para que borrar
el archivo pueda revertir ESE campo puntual (no la entidad entera), necesita
su propio rastro en el ledger — reusa `DataRepairItem` (antes que una tabla
nueva) porque `_ledger_restore.restore_from_before`/`entity_changed_since_ledger`
ya toleran un `before`/`after` PARCIAL (solo tocan los campos presentes en el
dict), sin modificarlas.

Acciones NUEVAS, no reusa `UPDATE_CUSTOMER`/`UPDATE_SUPPLIER`: esas asumen un
snapshot de la entidad ENTERA (`snapshot_master`) y las agrupa
`_revert_master_items` por (kind, entity_id) con semántica de "primer item =
antes del archivo, último = como lo dejó" — mezclar ahí un item de UN SOLO
campo cambiaría esa agrupación sin que el código lo supiera. F-D tiene su
propia función de reversión (7g), que lee sólo estas dos acciones.

`product:*` (compra→producto) todavía no captura (F-D 7b lo declaró fase
propia — acoplado al motor de costos de F-H6) así que no hay
`UPDATE_PRODUCT_CROSS_FIELD` todavía; se agrega cuando esa fase exista.

Additive y sin backfill: no hay campos cross escritos antes de esto.
"""

from __future__ import annotations

from alembic import op

revision = "20260816_0001"
down_revision = "20260815_0002"
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
    "CREATE_CUSTOMER",
    "UPDATE_CUSTOMER",
    "CREATE_SUPPLIER",
    "UPDATE_SUPPLIER",
)

_ACCIONES_NUEVAS = (
    "UPDATE_CUSTOMER_CROSS_FIELD",
    "UPDATE_SUPPLIER_CROSS_FIELD",
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
    # Es seguro — son items de ledger de campos cross que, al bajar esta
    # migración, el código tampoco sabría interpretar.
    valores = ",".join(f"'{a}'" for a in _ACCIONES_NUEVAS)
    op.execute(f"DELETE FROM {_TABLE} WHERE action IN ({valores})")  # noqa: S608
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_CONSTRAINT, _TABLE, _check(_ACCIONES_PREVIAS))
