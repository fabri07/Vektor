"""Unificación de los códigos de vertical al valor canónico largo.

Revision ID: 20260806_0002
Revises: 20260806_0001
Create Date: 2026-08-06

Contexto
--------
Hasta hoy convivían dos códigos para el mismo rubro: la base guardaba el código
CORTO (``'kiosco'``) mientras que los archivos de datos (heurísticas, campos de
vertical, categorías de producto) usaban el LARGO (``'kiosco_almacen'``). Los 11
sitios de fallback silencioso a kiosco tapaban la diferencia — y de paso hacían
que un negocio de limpieza se scoreara con los benchmarks de un kiosco.

Las Tasks 1-3 borraron esos fallbacks: ``app/domain/verticals.py`` es la única
fuente de verdad y ``parse_vertical`` **no aliasea nada** (``parse_vertical(
"kiosco")`` levanta ``UnknownVerticalError``). Esta migración es la que evita
que ese endurecimiento rompa producción: reescribe los datos al código largo
ANTES de que el código estricto los lea.

Orden (los tres pasos son funciones a nivel de módulo para que la suite de
integración Postgres las importe por ruta de archivo — mismo patrón que
``20260804_0001_product_name_normalized_notnull.py``):

1. ``_migrate_vertical_codes``: reescribe los alias legados al canónico en las
   TRES tablas que guardan un código de vertical.
2. ``_verify_verticals_canonical``: fail-safe de deploy. Si tras el paso 1 queda
   algún ``vertical_code`` fuera del catálogo, aborta con ``RuntimeError``
   listando los ofensores concretos. Railway corre las migraciones en
   ``preDeployCommand``: el deploy se corta y la versión vieja sigue sirviendo,
   en vez de dejar que un dato inesperado llegue al código estricto.
3. ``_add_vertical_check``: cierra con un CHECK a nivel DB para que no vuelva a
   entrar un código corto por ningún camino (import, script, SQL a mano).

Por qué las tres tablas, no solo ``business_profiles``
------------------------------------------------------
* ``business_profiles.vertical_code`` — el que leen heurísticas, categorías y
  FactsService.
* ``analytics_events.vertical_code`` — si no se migra,
  ``AnalyticsRepository.compute_margin_benchmark`` parte la muestra en dos
  (``'kiosco'`` vs ``'kiosco_almacen'``) y nunca alcanza su ``min_samples=5``:
  los benchmarks data-driven dejarían de computarse en silencio.
* ``heuristic_rule_sets.vertical`` — los rulesets ya se re-keyearon a códigos
  largos en el código (Task 2); las filas viejas de la DB tienen que acompañar.

Por qué ``updated_at`` se bumpea (es funcional, no cosmético)
-------------------------------------------------------------
``business_state_service._make_fingerprint`` incluye ``profile.updated_at`` en
el hash del estado cacheado en Redis: bumpearlo invalida los blobs viejos, que
fueron serializados con ``ruleset = 'kiosco'``. Es redundante con el bump de
clave a ``business_state:v2:`` que ya hizo la Task 2 — y está bien que lo sea,
son dos defensas independientes contra el mismo blob rancio.

Se usa ``CURRENT_TIMESTAMP`` y no ``now()`` porque es el estándar que entienden
tanto PostgreSQL (donde es exactamente ``now()``) como SQLite; la cadena Alembic
se aplica completa en ambos.

Literales hardcodeados a propósito
----------------------------------
No se importa ``OPERATIONAL_VERTICALS`` de ``app.domain.verticals``: una
migración es una foto del pasado y no debe seguir la evolución del enum (misma
decisión, y por el mismo motivo, que los CHECK literales de
``20260806_0001_access_requests.py``). Si mañana se agrega un cuarto vertical,
se agrega con una migración nueva que ensanche el CHECK.

``downgrade`` dropea el CHECK pero **NO revierte los datos**: reescribir
``'kiosco_almacen'`` a ``'kiosco'`` es imposible sin ambigüedad (no se puede
saber cuáles filas eran ``'kiosco'`` y cuáles ``'almacen'``), y además el código
post-Task-3 solo entiende el canónico. La reversión de esquema alcanza para
volver a desplegar la versión anterior.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260806_0002"
down_revision = "20260806_0001"
branch_labels = None
depends_on = None

#: Catálogo canónico (foto del pasado — ver docstring). Espejo de
#: ``app.domain.verticals.OPERATIONAL_VERTICALS`` al momento de esta migración.
_CANONICOS: tuple[str, ...] = ("kiosco_almacen", "decoracion_hogar", "limpieza")

#: Código canónico → códigos legados que hay que reescribir a él. Solo alias
#: HISTÓRICOS reales: nunca se adivina a qué rubro pertenece un código que no
#: esté en esta tabla (no-invention). Un código desconocido no se "arregla", se
#: reporta y corta el deploy en ``_verify_verticals_canonical``.
_ALIAS_LEGADOS: dict[str, tuple[str, ...]] = {
    "kiosco_almacen": ("kiosco", "almacen"),
    "decoracion_hogar": ("deco", "deco_hogar", "decoracion"),
    "limpieza": ("cleaning",),
}

#: (tabla, columna, ¿tiene ``updated_at`` que bumpear?)
_TABLAS_CON_VERTICAL: tuple[tuple[str, str, bool], ...] = (
    ("business_profiles", "vertical_code", True),
    ("analytics_events", "vertical_code", False),
    ("heuristic_rule_sets", "vertical", False),
)

_CHECK_NAME = "ck_business_profiles_vertical_code"


def _tabla_existe(bind: sa.Connection, nombre: str) -> bool:
    """¿Existe la tabla que resolvería una query SIN calificar?

    En PostgreSQL se usa ``to_regclass``, que resuelve por ``search_path`` (o
    sea: exactamente el mismo nombre que van a ver los ``UPDATE``/``SELECT`` de
    abajo). NO alcanza con ``sa.inspect(bind).get_table_names()``: filtra por
    ``relpersistence <> 't'`` y por ``pg_table_is_visible``, así que (a) nunca
    ve una tabla TEMPORAL y (b) deja de ver la tabla de ``public`` en cuanto una
    temporal homónima la sombrea. Eso rompía la suite de integración, que
    ejercita estas funciones sobre tablas temporales para no tocar los datos
    reales. En el resto de los dialectos se cae al inspector de siempre.
    """
    if bind.dialect.name == "postgresql":
        resuelta = bind.execute(sa.text("SELECT to_regclass(:n)"), {"n": nombre}).scalar()
        return resuelta is not None
    return nombre in set(sa.inspect(bind).get_table_names())


def _migrate_vertical_codes(bind: sa.Connection) -> None:
    """Reescribe los códigos legados al canónico en las tres tablas.

    Idempotente por construcción: el ``WHERE`` filtra por los alias legados, así
    que una segunda corrida no matchea ninguna fila. Las tablas se guardan por
    existencia — ninguna es opcional en la cadena, pero una migración que asume
    tablas ajenas y explota deja el deploy roto por un motivo equivocado.
    """
    for tabla, columna, bumpea_updated_at in _TABLAS_CON_VERTICAL:
        if not _tabla_existe(bind, tabla):
            continue
        for canonico, alias in _ALIAS_LEGADOS.items():
            marcadores = ", ".join(f":a{i}" for i in range(len(alias)))
            params: dict[str, str] = {f"a{i}": valor for i, valor in enumerate(alias)}
            params["canonico"] = canonico
            set_clause = f"{columna} = :canonico"
            if bumpea_updated_at:
                # Invalida el fingerprint del caché de estado (ver docstring).
                set_clause += ", updated_at = CURRENT_TIMESTAMP"
            bind.execute(
                sa.text(
                    f"UPDATE {tabla} SET {set_clause} "  # noqa: S608 - nombres fijos del módulo
                    f"WHERE {columna} IN ({marcadores})"
                ),
                params,
            )


def _verify_verticals_canonical(bind: sa.Connection) -> None:
    """Aborta la migración si queda algún vertical fuera del catálogo canónico.

    Es el fail-safe de deploy, no un adorno: Railway corre las migraciones en
    ``preDeployCommand``, así que este ``RuntimeError`` aborta el deploy y la
    versión vieja (la que todavía tolera el dato raro) sigue sirviendo. Es la
    última red antes de que un código desconocido llegue a ``parse_vertical``,
    que a partir de la Task 1 levanta en vez de degradar a kiosco.

    Lista los ofensores CONCRETOS (código + cantidad de filas) para que el
    operador pueda decidir a mano a qué vertical corresponden — nunca se
    adivina desde acá (no-invention).
    """
    if not _tabla_existe(bind, "business_profiles"):
        return

    marcadores = ", ".join(f":c{i}" for i in range(len(_CANONICOS)))
    params = {f"c{i}": valor for i, valor in enumerate(_CANONICOS)}
    filas = bind.execute(
        sa.text(
            "SELECT vertical_code, count(*) AS total FROM business_profiles "
            f"WHERE vertical_code NOT IN ({marcadores}) "  # noqa: S608 - marcadores ligados
            "GROUP BY vertical_code ORDER BY vertical_code"
        ),
        params,
    ).fetchall()

    if not filas:
        return

    detalle = ", ".join(f"{codigo!r} ({total} perfil/es)" for codigo, total in filas)
    validos = ", ".join(repr(v) for v in _CANONICOS)
    raise RuntimeError(
        "MIGRACIÓN ABORTADA: business_profiles tiene vertical_code fuera del "
        f"catálogo canónico: {detalle}. Válidos: {validos}. No se adivina a qué "
        "rubro corresponden (no-invention). Corré "
        "scripts/preflight_vertical_codes.py para el detalle por tabla y "
        "corregí esas filas antes de reintentar el deploy."
    )


def _check_existe(bind: sa.Connection) -> bool:
    """¿Ya está puesta la CHECK sobre la ``business_profiles`` que resuelve el
    ``search_path``? En PostgreSQL se filtra por ``conrelid`` para no confundir
    una constraint homónima de otra tabla."""
    if bind.dialect.name == "postgresql":
        fila = bind.execute(
            sa.text(
                "SELECT 1 FROM pg_constraint "
                "WHERE conname = :n AND conrelid = to_regclass('business_profiles')"
            ),
            {"n": _CHECK_NAME},
        ).first()
        return fila is not None
    try:
        return any(
            c.get("name") == _CHECK_NAME
            for c in sa.inspect(bind).get_check_constraints("business_profiles")
        )
    except NotImplementedError:  # pragma: no cover - dialecto sin introspección
        return False


def _add_vertical_check(bind: sa.Connection) -> None:
    """Cierra con un CHECK: ``business_profiles.vertical_code`` solo canónicos.

    En PostgreSQL se usa el patrón zero-lock de ``20260804_0001``: ``ADD
    CONSTRAINT ... NOT VALID`` (instantáneo, no escanea ni bloquea) seguido de
    ``VALIDATE CONSTRAINT`` (solo SHARE UPDATE EXCLUSIVE, no bloquea lecturas ni
    escrituras concurrentes). Importa porque el ``preDeployCommand`` corre con
    la versión vieja todavía sirviendo tráfico.

    Idempotente: si la constraint ya existe, no hace nada.
    """
    if not _tabla_existe(bind, "business_profiles"):
        return
    if _check_existe(bind):
        return

    predicado = "vertical_code IN (" + ", ".join(f"'{v}'" for v in _CANONICOS) + ")"

    if bind.dialect.name != "postgresql":
        # SQLite (tests): sin locking real que evitar. `batch_alter_table` recrea
        # la tabla, que es como SQLite soporta agregar un CHECK.
        with op.batch_alter_table("business_profiles") as batch_op:
            batch_op.create_check_constraint(_CHECK_NAME, predicado)
        return

    bind.execute(
        sa.text(
            f"ALTER TABLE business_profiles ADD CONSTRAINT {_CHECK_NAME} "
            f"CHECK ({predicado}) NOT VALID"
        )
    )
    bind.execute(sa.text(f"ALTER TABLE business_profiles VALIDATE CONSTRAINT {_CHECK_NAME}"))


def upgrade() -> None:
    bind = op.get_bind()
    _migrate_vertical_codes(bind)
    _verify_verticals_canonical(bind)
    _add_vertical_check(bind)


def downgrade() -> None:
    bind = op.get_bind()
    if not _tabla_existe(bind, "business_profiles"):
        return

    if bind.dialect.name == "postgresql":
        bind.execute(
            sa.text(f"ALTER TABLE business_profiles DROP CONSTRAINT IF EXISTS {_CHECK_NAME}")
        )
        return

    if _check_existe(bind):
        with op.batch_alter_table("business_profiles") as batch_op:
            batch_op.drop_constraint(_CHECK_NAME, type_="check")
