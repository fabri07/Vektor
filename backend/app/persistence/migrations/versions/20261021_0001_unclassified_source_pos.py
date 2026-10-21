"""B·5e-2: «Otros» acepta tareas del POS (``source='pos'``).

Una venta del POS sin red ya cobrada, de un producto sin compra ni stock inicial
a su fecha, entra completa (la plata y el turno son reales) y deja en «Otros» una
tarea para cargar la compra. Hasta acá el CHECK sólo admitía ``ingestion``,
``chat`` y ``reanalysis``.

Additive: el predicado nuevo es un superconjunto del viejo; ``NOT VALID`` +
``VALIDATE CONSTRAINT`` para no bloquear con la versión vieja sirviendo tráfico
(mismo patrón que ``20260917_0001``). El ``downgrade`` restringe al vocabulario
viejo: si hay tareas del POS, no valida y falla ruidosamente (la alternativa
sería borrar el dato).
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261021_0001"
down_revision = "20261019_0001"
branch_labels = None
depends_on = None

_TABLA = "unclassified_records"
_COLUMNA = "source"
_CONSTRAINT = "ck_unclassified_records_source"

#: Válidos DESPUÉS de esta migración.
_PLANES = ("ingestion", "chat", "reanalysis", "pos")
#: Válidos ANTES (para el downgrade).
_PLANES_PREVIOS = ("ingestion", "chat", "reanalysis")


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
    return f"{_COLUMNA} IN ({lista})"


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
