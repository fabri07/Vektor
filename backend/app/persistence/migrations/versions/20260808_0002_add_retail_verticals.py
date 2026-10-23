"""Ensanchar los CHECK de vertical para los 3 rubros de reventa nuevos.

Agrega ``libreria_papeleria``, ``indumentaria`` y ``verduleria_fruteria`` al
catálogo canónico. Los CHECK que enumeran códigos son literales **por decisión**
(ver ``20260806_0002``): no se importan de ``app.domain.verticals`` porque una
migración describe el estado de la base en un momento dado, no el del código en
el momento del deploy. Por eso sumar un rubro pasa por escribir una migración
que ensanche el predicado, y eso es deliberado.

Tres constraints enumeran códigos de vertical:

* ``ck_business_profiles_vertical_code`` — el rubro de un negocio ya dado de alta.
* ``ck_access_requests_requested_vertical`` — el rubro DECLARADO en la solicitud
  pública (incluye ``'otros'``, que nunca es operativo).
* ``ck_access_requests_assigned_vertical_code`` — el rubro ASIGNADO al aprobar
  (solo operativos: ``'otros'`` es inescribible acá).

Additive: ningún registro existente cambia de valor y el predicado nuevo es un
superconjunto del viejo, así que toda fila que pasaba sigue pasando. Se puede
aplicar con la versión anterior sirviendo tráfico.

``downgrade`` restringe el predicado a los 3 rubros originales. Si para entonces
hay negocios en un rubro nuevo, la constraint no valida y el downgrade falla
ruidosamente — que es lo correcto: la alternativa sería borrarles el rubro.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260808_0002"
down_revision = "20260808_0001"
branch_labels = None
depends_on = None

#: Operativos DESPUÉS de esta migración.
_OPERATIVOS = (
    "kiosco_almacen",
    "decoracion_hogar",
    "limpieza",
    "libreria_papeleria",
    "indumentaria",
    "verduleria_fruteria",
)
#: Operativos ANTES (para el downgrade).
_OPERATIVOS_PREVIOS = ("kiosco_almacen", "decoracion_hogar", "limpieza")

#: (tabla, columna, nombre de la constraint, incluye 'otros')
_CHECKS: tuple[tuple[str, str, str, bool], ...] = (
    ("business_profiles", "vertical_code", "ck_business_profiles_vertical_code", False),
    ("access_requests", "requested_vertical", "ck_access_requests_requested_vertical", True),
    (
        "access_requests",
        "assigned_vertical_code",
        "ck_access_requests_assigned_vertical_code",
        False,
    ),
)


def _tabla_existe(bind: sa.Connection, tabla: str) -> bool:
    return sa.inspect(bind).has_table(tabla)


def _check_existe(bind: sa.Connection, tabla: str, nombre: str) -> bool:
    if bind.dialect.name == "postgresql":
        fila = bind.execute(
            sa.text(
                "SELECT 1 FROM pg_constraint "
                "WHERE conname = :n AND conrelid = to_regclass(:t)"
            ),
            {"n": nombre, "t": tabla},
        ).first()
        return fila is not None
    try:
        return any(
            c.get("name") == nombre for c in sa.inspect(bind).get_check_constraints(tabla)
        )
    except NotImplementedError:  # pragma: no cover - dialecto sin introspección
        return False


def _predicado(columna: str, codigos: tuple[str, ...], *, nullable: bool, con_otros: bool) -> str:
    valores = (*codigos, "otros") if con_otros else codigos
    lista = ", ".join(f"'{v}'" for v in valores)
    if nullable:
        # `assigned_vertical_code` es NULL mientras la solicitud no se aprueba.
        return f"{columna} IS NULL OR {columna} IN ({lista})"
    return f"{columna} IN ({lista})"


def _recrear_checks(bind: sa.Connection, codigos: tuple[str, ...]) -> None:
    for tabla, columna, nombre, con_otros in _CHECKS:
        if not _tabla_existe(bind, tabla):
            continue
        nullable = columna == "assigned_vertical_code"
        predicado = _predicado(columna, codigos, nullable=nullable, con_otros=con_otros)

        if bind.dialect.name != "postgresql":
            # SQLite (tests): `batch_alter_table` recrea la tabla, que es como
            # SQLite soporta cambiar un CHECK.
            with op.batch_alter_table(tabla) as batch_op:
                if _check_existe(bind, tabla, nombre):
                    batch_op.drop_constraint(nombre, type_="check")
                batch_op.create_check_constraint(nombre, predicado)
            continue

        if _check_existe(bind, tabla, nombre):
            bind.execute(sa.text(f"ALTER TABLE {tabla} DROP CONSTRAINT {nombre}"))
        # Mismo patrón zero-lock que `20260806_0002`: NOT VALID es instantáneo y
        # no bloquea, VALIDATE toma solo SHARE UPDATE EXCLUSIVE. Importa porque
        # el preDeployCommand corre con la versión vieja sirviendo tráfico.
        bind.execute(
            sa.text(
                f"ALTER TABLE {tabla} ADD CONSTRAINT {nombre} CHECK ({predicado}) NOT VALID"
            )
        )
        bind.execute(sa.text(f"ALTER TABLE {tabla} VALIDATE CONSTRAINT {nombre}"))


def upgrade() -> None:
    _recrear_checks(op.get_bind(), _OPERATIVOS)


def downgrade() -> None:
    _recrear_checks(op.get_bind(), _OPERATIVOS_PREVIOS)
