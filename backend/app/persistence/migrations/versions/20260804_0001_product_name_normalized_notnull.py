"""Product.name_normalized NOT NULL (Fase 8-D — contract)

Revision ID: 20260804_0001
Revises: 20260803_0001
Create Date: 2026-08-04

Contexto
--------
``products.name_normalized`` existe desde F2/F5, nullable, y el listener
``_sync_product_identity_columns`` (``app/persistence/models/product.py``) la
llena en TODO insert/update desde entonces — la fase *expand* ya está desplegada
y viva en prod. Esta migración cierra el *contract*: `NOT NULL` real en la
columna.

**Recorte fijado (F8d):** SOLO ``Product.name_normalized``. NO toca
SKU/barcode (``sku_normalized``/``barcode_normalized`` siguen nullable — no
todo producto tiene código) ni ``Customer``/``Supplier``.

Orden (mismo patrón que ``20260731_0002_backfill_product_identity.py`` +
``20260802_0001_product_identity_unique_indexes.py``):

1. ``_run_backfill``: recorre cualquier straggler (``name_normalized IS NULL``)
   que el listener no haya tocado — batch de a ``_BATCH``, recalcula con el
   normalizador canónico (``app.domain.text_norm.normalize_product_name``,
   NUNCA en SQL). Idempotente: el filtro ``IS NULL`` reduce estrictamente el
   conjunto en cada pasada.
2. ``_verify_clean``: cuenta las filas que impedirían el ``NOT NULL``
   (``name_normalized`` nulo o vacío tras el backfill). Si hay alguna, la fila
   tiene un ``name`` basura/ilegible del que no se puede derivar un valor real
   (no-invention) — se **aborta la migración** con ``RuntimeError`` en vez de
   forzar el NOT NULL sobre datos sucios. Fail-safe de Railway: el deploy se
   corta y la versión vieja sigue sirviendo. El operador corre
   ``scripts/preflight_product_name_normalized.py`` para ubicar y reparar el
   ``name`` de esas filas antes de reintentar.
3. ``SET NOT NULL`` — SIN ``server_default`` (un default no protege contra
   ``''``, que es exactamente lo que ``_verify_clean`` ya descartó que exista).
   En Postgres, en vez de un ``ALTER COLUMN ... SET NOT NULL`` directo (ACCESS
   EXCLUSIVE + full table scan bajo tráfico vivo del preDeploy — el mismo
   problema que documenta ``20260802_0001``), se usa el patrón zero-lock:
   ``ADD CONSTRAINT ... CHECK (...) NOT VALID`` (instantáneo) → ``VALIDATE
   CONSTRAINT`` (solo SHARE UPDATE EXCLUSIVE, no bloquea lecturas/escrituras) →
   ``SET NOT NULL`` (PG 12+ lo resuelve instantáneo porque la CHECK ya
   validada prueba el NOT NULL, sin escanear de nuevo) → ``DROP CONSTRAINT``
   (la CHECK queda redundante una vez que el NOT NULL está puesto; catalog-only,
   instantáneo). En otros dialectos (SQLite, tests) se usa el
   ``alter_column(nullable=False)`` simple.

``downgrade`` revierte la columna a nullable (reversible; no re-vacía datos).

Funciones a nivel de módulo (no anidadas en ``upgrade``) para que la suite de
integración Postgres (T2, ``app/tests/integration/``) las importe por ruta de
archivo y las ejercite contra datos sucios reales — mismo patrón que
``_run_preflight``/``_ensure_index`` de ``20260802_0001``.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from app.domain.text_norm import normalize_product_name

revision = "20260804_0001"
down_revision = "20260803_0001"
branch_labels = None
depends_on = None

_BATCH = 500

_CHECK_NAME = "ck_products_name_normalized_not_null"


def _run_backfill(bind: sa.Connection) -> None:
    """Recalcula ``name_normalized`` de cualquier straggler que el listener no
    haya tocado (fila legacy nunca re-escrita desde que existe la columna).

    ``normalize_product_name`` siempre devuelve un ``str`` (``""`` en el peor
    caso), así que cada fila procesada deja de matchear ``IS NULL`` — no hace
    falta ``OFFSET``, basta con releer el tope hasta agotar el conjunto.
    """
    select_stragglers = sa.text(
        "SELECT id, name FROM products WHERE name_normalized IS NULL "
        "ORDER BY id LIMIT :batch"
    )
    update_row = sa.text("UPDATE products SET name_normalized = :name_n WHERE id = :id")

    while True:
        rows = bind.execute(select_stragglers, {"batch": _BATCH}).fetchall()
        if not rows:
            break
        for pid, name in rows:
            bind.execute(update_row, {"id": pid, "name_n": normalize_product_name(name)})


def _verify_clean(bind: sa.Connection) -> None:
    """Aborta la migración si queda alguna fila que el ``NOT NULL`` rechazaría.

    Tras ``_run_backfill`` no debería quedar ninguna: si queda, es porque el
    ``name`` crudo es basura/ilegible (vacío, solo espacios, solo diacríticos)
    y ``normalize_product_name`` no tiene de dónde derivar un valor real. No se
    inventa un nombre — se corta el deploy (no-invention).
    """
    count = bind.execute(
        sa.text(
            "SELECT count(*) FROM products "
            "WHERE name_normalized IS NULL OR trim(name_normalized) = ''"
        )
    ).scalar_one()
    if count > 0:
        raise RuntimeError(
            "MIGRACIÓN ABORTADA: "
            f"{count} producto(s) con name_normalized nulo o vacío — el nombre "
            "es basura/ilegible y no se puede derivar un valor (no-invention). "
            "Corré scripts/preflight_product_name_normalized.py para listarlos y "
            "reparar el name antes de reintentar el deploy."
        )


def upgrade() -> None:
    bind = op.get_bind()
    _run_backfill(bind)
    _verify_clean(bind)

    if bind.dialect.name != "postgresql":
        # SQLite (tests): sin locking real que evitar, alcanza con el ALTER
        # directo. `batch_alter_table` recrea la tabla, que es como SQLite
        # soporta modificar NOT NULL de una columna existente.
        with op.batch_alter_table("products") as batch_op:
            batch_op.alter_column(
                "name_normalized",
                existing_type=sa.String(400),
                nullable=False,
            )
        return

    # Postgres: patrón zero-lock (mismo motivo que 20260802_0001). Un
    # `ALTER COLUMN ... SET NOT NULL` directo toma ACCESS EXCLUSIVE + escanea
    # toda la tabla bajo el tráfico vivo del preDeploy. En vez de eso:
    #   1) CHECK NOT VALID — instantáneo, no escanea.
    #   2) VALIDATE CONSTRAINT — solo SHARE UPDATE EXCLUSIVE, no bloquea
    #      lecturas/escrituras concurrentes.
    #   3) SET NOT NULL — PG 12+ lo resuelve instantáneo porque la CHECK ya
    #      validada prueba el NOT NULL, sin volver a escanear.
    #   4) DROP CONSTRAINT — la CHECK queda redundante con el NOT NULL puesto;
    #      catalog-only, instantáneo.
    bind.execute(
        sa.text(
            f"ALTER TABLE products ADD CONSTRAINT {_CHECK_NAME} "
            "CHECK (name_normalized IS NOT NULL) NOT VALID"
        )
    )
    bind.execute(sa.text(f"ALTER TABLE products VALIDATE CONSTRAINT {_CHECK_NAME}"))
    bind.execute(sa.text("ALTER TABLE products ALTER COLUMN name_normalized SET NOT NULL"))
    bind.execute(sa.text(f"ALTER TABLE products DROP CONSTRAINT {_CHECK_NAME}"))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # Defensivo: si un upgrade quedó a mitad de camino (falló entre el ADD
        # y el DROP), no dejar la CHECK huérfana al revertir.
        bind.execute(sa.text(f"ALTER TABLE products DROP CONSTRAINT IF EXISTS {_CHECK_NAME}"))
        op.alter_column(
            "products",
            "name_normalized",
            existing_type=sa.String(400),
            nullable=True,
        )
        return

    with op.batch_alter_table("products") as batch_op:
        batch_op.alter_column(
            "name_normalized",
            existing_type=sa.String(400),
            nullable=True,
        )
