"""Ensanchar el CHECK de requested_plan con los 3 planes pagos reales.

Agrega ``esencial``, ``control`` y ``direccion`` al vocabulario cerrado de
`RequestedPlan` (`app.domain.access_request`). La página pública de precios deja
de ofrecer ``premium`` como opción (reemplazado por estos tres), pero el valor
sigue siendo válido acá por compatibilidad con solicitudes históricas — ver el
docstring de `RequestedPlan` en el dominio.

``requested_plan`` es un ``VARCHAR(20)`` + CHECK (no hay enum nativo de
Postgres en esta tabla — ver `20260806_0001`), así que el patrón es el mismo
que `20260808_0002`: ``NOT VALID`` + ``VALIDATE CONSTRAINT`` para no bloquear
con el `preDeployCommand` corriendo contra la versión vieja sirviendo tráfico.

Additive: ningún registro existente cambia de valor, el predicado nuevo es un
superconjunto del viejo. Aprobar una solicitud sigue creando siempre una
suscripción ``FREE`` — este cambio no toca `tenant_provisioning.py` ni activa
ningún cobro; ver la política de planes en ``docs/plans/`` para la hoja de ruta
completa.

``downgrade`` restringe el predicado a los 2 valores originales. Si para
entonces hay solicitudes con un plan nuevo, la constraint no valida y el
downgrade falla ruidosamente — correcto: la alternativa sería borrar el dato.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260917_0001"
down_revision = "20260910_0005"
branch_labels = None
depends_on = None

_TABLA = "access_requests"
_COLUMNA = "requested_plan"
_CONSTRAINT = "ck_access_requests_requested_plan"

#: Válidos DESPUÉS de esta migración.
_PLANES = ("free", "premium", "esencial", "control", "direccion")
#: Válidos ANTES (para el downgrade).
_PLANES_PREVIOS = ("free", "premium")


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
