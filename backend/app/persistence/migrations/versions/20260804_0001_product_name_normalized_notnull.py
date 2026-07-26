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
3. ``op.alter_column(..., nullable=False)`` — SIN ``server_default`` (un
   default no protege contra ``''``, que es exactamente lo que ``_verify_clean``
   ya descartó que exista).

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
    op.alter_column(
        "products",
        "name_normalized",
        existing_type=sa.String(400),
        nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "products",
        "name_normalized",
        existing_type=sa.String(400),
        nullable=True,
    )
