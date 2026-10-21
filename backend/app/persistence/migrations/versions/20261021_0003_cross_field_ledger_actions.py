"""B·6: acciones de ledger para los campos de ficha completados desde otra sección.

Una hoja de ventas puede completar la ficha del cliente (apellido, domicilio,
condición de IVA…) y una de compras la del proveedor (apellido, forma de pago).
Para que borrar el archivo devuelva ESE campo a como estaba, cada campo escrito
deja su ítem en ``data_repair_items`` con su antes y su después.

Acciones NUEVAS y no ``UPDATE_CUSTOMER``/``UPDATE_SUPPLIER`` (decisión del
dueño, 2026-10-20): esas guardan la ficha ENTERA y su reversa la conserva entera
si el cliente tiene ventas de otra fuente, así que el campo que trajo el archivo
no volvería nunca. Las nuevas las lee sólo ``_revert_cross_field_items``, que
revierte campo por campo.

Additive (superconjunto del CHECK viejo) y sin backfill: antes de esto ningún
import escribía estos campos. Idempotente: el CHECK se recrea sólo si existe la
tabla, y el ``DROP`` sólo si existe el constraint. El ``downgrade`` borra antes
las filas con las acciones nuevas (violarían el CHECK viejo y el código anterior
no sabría interpretarlas).
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261021_0003"
down_revision = "20261021_0002"
branch_labels = None
depends_on = None

_TABLA = "data_repair_items"
_CONSTRAINT = "ck_repair_items_action"

#: Válidas ANTES de esta migración (las de `20260810_0001`).
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


def _tabla_existe(bind: sa.Connection) -> bool:
    return sa.inspect(bind).has_table(_TABLA)


def _check_existe(bind: sa.Connection) -> bool:
    if bind.dialect.name == "postgresql":
        fila = bind.execute(
            sa.text(
                "SELECT 1 FROM pg_constraint "
                "WHERE conname = :n AND conrelid = to_regclass(:t)"
            ),
            {"n": _CONSTRAINT, "t": _TABLA},
        ).first()
        return fila is not None
    try:
        return any(
            c.get("name") == _CONSTRAINT
            for c in sa.inspect(bind).get_check_constraints(_TABLA)
        )
    except NotImplementedError:  # pragma: no cover - dialecto sin introspección
        return False


def _predicado(acciones: tuple[str, ...]) -> str:
    valores = ",".join(f"'{a}'" for a in acciones)
    return f"action IN ({valores})"


def _recrear_check(bind: sa.Connection, acciones: tuple[str, ...]) -> None:
    if not _tabla_existe(bind):
        return
    predicado = _predicado(acciones)

    if bind.dialect.name != "postgresql":
        # SQLite (tests): `batch_alter_table` recrea la tabla, que es como
        # SQLite soporta cambiar un CHECK.
        with op.batch_alter_table(_TABLA) as batch_op:
            if _check_existe(bind):
                batch_op.drop_constraint(_CONSTRAINT, type_="check")
            batch_op.create_check_constraint(_CONSTRAINT, predicado)
        return

    if _check_existe(bind):
        bind.execute(sa.text(f"ALTER TABLE {_TABLA} DROP CONSTRAINT {_CONSTRAINT}"))
    bind.execute(
        sa.text(
            f"ALTER TABLE {_TABLA} ADD CONSTRAINT {_CONSTRAINT} CHECK ({predicado}) NOT VALID"
        )
    )
    bind.execute(sa.text(f"ALTER TABLE {_TABLA} VALIDATE CONSTRAINT {_CONSTRAINT}"))


def upgrade() -> None:
    _recrear_check(op.get_bind(), _ACCIONES_PREVIAS + _ACCIONES_NUEVAS)


def downgrade() -> None:
    bind = op.get_bind()
    if _tabla_existe(bind):
        valores = ",".join(f"'{a}'" for a in _ACCIONES_NUEVAS)
        bind.execute(sa.text(f"DELETE FROM {_TABLA} WHERE action IN ({valores})"))  # noqa: S608
    _recrear_check(bind, _ACCIONES_PREVIAS)
