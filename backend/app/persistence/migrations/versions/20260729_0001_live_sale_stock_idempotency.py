"""Idempotencia de descuento de venta en vivo: source_event_id único por venta.

Revision ID: 20260729_0001
Revises: 20260728_0001
Create Date: 2026-07-29

Contexto
--------
Las ventas EN VIVO ahora descuentan stock por un único helper idempotente
(``stock_service.decrement_for_sale``) que corre tanto síncrono en la transacción del
request como en el backstop por evento ``SALE_RECORDED`` (otra sesión, async). Para
que "ambos caminos" no doble-descuenten aún ante la carrera (el async no ve lo que el
sync todavía no commiteó), la idempotencia real vive a nivel DB: un índice único
parcial sobre ``(tenant_id, source_event_id)`` para los movimientos ``sale`` vivos.
El 2º INSERT concurrente falla con ``IntegrityError`` y el helper lo trata como no-op.

Cada movimiento de venta usa ``source_event_id = 'sale:{sale_entry.id}'`` (uno por
venta/línea) → habilita además la reversa exacta al anular/editar una venta.

PASO PREVIO EN LA MISMA MIGRACIÓN — backfill del formato legacy
--------------------------------------------------------------
Antes de este cambio, ``manual-batch`` grababa ``source_event_id = 'manual_batch:{group}'``
COMPARTIDO por todas las líneas del mismo grupo. Con varias líneas por grupo, esas
filas colisionan bajo el índice único → habría que resolverlas antes de crearlo. El
backfill las reescribe a ``'sale:{sale_entry.id}'`` matcheando cada movimiento con su
``SaleEntry`` por ``(tenant_id, product_id, sale_group_id)`` — único porque manual-batch
rechaza productos repetidos dentro de un mismo carrito. Solo entonces se crea el índice.

Si tras el backfill quedaran duplicados vivos (datos anómalos no matcheables), la
creación del índice FALLA y aborta el deploy (fail-safe) — hay que reconciliarlos a mano.
Verificación previa recomendada contra Neon: contar movimientos ``sale`` vivos con
``source_event_id`` duplicado por ``(tenant_id, source_event_id)`` antes de mergear.

Solo aplica a Postgres (SQLite, en tests, ignora índices parciales y no tiene datos
legacy). Idempotente vía ``sa.inspect`` (varios servicios de Railway pueden correr
``alembic upgrade head`` en paralelo en un mismo deploy).
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260729_0001"
down_revision = "20260728_0001"
branch_labels = None
depends_on = None

_TABLE = "inventory_movements"
_INDEX = "uq_inventory_movements_live_sale_event"

# Reescribe manual_batch:{group} → sale:{sale_entry.id} matcheando por tenant+producto+grupo.
_BACKFILL = sa.text(
    """
    UPDATE inventory_movements AS m
    SET source_event_id = 'sale:' || s.id
    FROM sales_entries AS s
    WHERE m.movement_type = 'sale'
      AND m.source_event_id LIKE 'manual_batch:%'
      AND s.tenant_id = m.tenant_id
      AND s.product_id = m.product_id
      AND s.custom_fields ->> 'sale_group_id'
          = substring(m.source_event_id FROM 'manual_batch:(.*)')
    """
)

# Acotado a las claves de VENTA EN VIVO ('sale:{id}', gestionadas por
# stock_service.decrement_for_sale). Deja FUERA los movimientos 'sale' que crea
# UPDATE_STOCK vía decrement_stock (source_event_id = str(action.id), sin prefijo): no
# pasan por el helper idempotente y no deben quedar bajo esta unicidad.
_CREATE_INDEX = sa.text(
    f"CREATE UNIQUE INDEX {_INDEX} ON {_TABLE} (tenant_id, source_event_id) "
    "WHERE movement_type = 'sale' AND voided_at IS NULL AND source_event_id LIKE 'sale:%'"
)

# Colisiones que romperían el índice: movimientos 'sale' vivos 'sale:%' con
# source_event_id repetido por tenant (mismo predicado que el índice).
_COUNT_DUPES = sa.text(
    "SELECT COALESCE(SUM(c - 1), 0) FROM ("
    "  SELECT COUNT(*) AS c FROM inventory_movements"
    "  WHERE movement_type = 'sale' AND voided_at IS NULL AND source_event_id LIKE 'sale:%'"
    "  GROUP BY tenant_id, source_event_id HAVING COUNT(*) > 1"
    ") d"
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    inspector = sa.inspect(bind)
    existing = {ix["name"] for ix in inspector.get_indexes(_TABLE)}
    if _INDEX in existing:
        return
    op.execute(_BACKFILL)
    # Validar ANTES de crear el índice: un CREATE UNIQUE INDEX que falla da un error
    # de Postgres críptico; este chequeo aborta el deploy con un mensaje accionable.
    dupes = bind.execute(_COUNT_DUPES).scalar() or 0
    if int(dupes) > 0:
        raise RuntimeError(
            f"No se puede crear {_INDEX}: quedan {dupes} movimiento(s) 'sale' vivos con "
            "source_event_id duplicado por tenant tras el backfill de manual_batch. "
            "Reconciliar a mano (probablemente grupos manual_batch sin sale_group_id en "
            "custom_fields) antes de reintentar el deploy."
        )
    op.execute(_CREATE_INDEX)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    inspector = sa.inspect(bind)
    existing = {ix["name"] for ix in inspector.get_indexes(_TABLE)}
    if _INDEX in existing:
        op.drop_index(_INDEX, table_name=_TABLE)
