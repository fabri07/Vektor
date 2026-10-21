"""B·5g: ``sales_entries.void_reason`` acepta ``REPAIR_SALE_WITHOUT_PURCHASE``.

La reparación explícita (``scripts/ventas_sin_compra_a_otros.py --apply``, la
autoriza el dueño) anula las ventas históricas sin producto o sin compra previa
y deja su fila en «Otros»; el motivo de anulación dice cuál fue.

Additive (superconjunto del CHECK viejo), ``NOT VALID`` + ``VALIDATE``. El
``downgrade`` restringe al vocabulario viejo: con ventas reparadas no valida y
falla ruidosamente.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261021_0002"
down_revision = "20261021_0001"
branch_labels = None
depends_on = None

_TABLA = "sales_entries"
_COLUMNA = "void_reason"
_CONSTRAINT = "ck_sales_entries_void_reason"

#: Válidos DESPUÉS de esta migración.
_PLANES = (
    "REPAIR_MISCLASSIFIED_IMPORT", "USER_CANCELLED", "DUPLICATE", "MANUAL_ADMIN_VOID",
    "REREAD_REIMPORT", "REPAIR_SALE_WITHOUT_PURCHASE",
)
#: Válidos ANTES (para el downgrade).
_PLANES_PREVIOS = (
    "REPAIR_MISCLASSIFIED_IMPORT", "USER_CANCELLED", "DUPLICATE", "MANUAL_ADMIN_VOID",
    "REREAD_REIMPORT",
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


def _predicado(planes: tuple[str, ...]) -> str:
    lista = ", ".join(f"'{v}'" for v in planes)
    return f"{_COLUMNA} IS NULL OR {_COLUMNA} IN ({lista})"


def _recrear_check(bind: sa.Connection, planes: tuple[str, ...]) -> None:
    if not _tabla_existe(bind):
        return
    predicado = _predicado(planes)

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
    _recrear_check(op.get_bind(), _PLANES)


def downgrade() -> None:
    _recrear_check(op.get_bind(), _PLANES_PREVIOS)
