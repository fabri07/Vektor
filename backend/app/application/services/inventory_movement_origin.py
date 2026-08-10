"""Fuente ÚNICA de verdad del origen de un ``InventoryMovement``.

Toda la app (ingesta, reread, script de reparación, repos) importa de acá la
semántica de ``source_type``, el algoritmo de ``source_row_hash`` y los helpers de
``voided_at``. Nadie reimplementa su propia interpretación — así el ledger de
inventario queda consistente de punta a punta.

- ``source_type``: qué generó el movimiento (dedup, reversa y reportes).
- ``source_row_hash``: identidad lógica ESTABLE de la fila de origen (no depende del
  orden del Excel). Dos importaciones de la misma fila producen el mismo hash → clave
  de idempotencia.
"""

from __future__ import annotations

import hashlib
import unicodedata
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

# ── source_type: valores canónicos ──────────────────────────────────────────────
SOURCE_PURCHASE_IMPORT = "purchase_import"          # libro de compras / gasto de mercadería
SOURCE_CATALOG_INITIAL_STOCK = "catalog_initial_stock"  # stock inicial de un catálogo
SOURCE_MANUAL_ADJUSTMENT = "manual_adjustment"      # ajuste manual de stock
# RESERVADO: hoy ningún código usa este valor — UPDATE_STOCK vía chat produce
# movement_type='purchase'/'sale' en stock_service.py, nunca 'adjustment' (ver
# pending_action_service.py). Cuando exista un ajuste manual real (chat o
# dashboard), ese writer DEBE setear source_type=SOURCE_MANUAL_ADJUSTMENT + un
# actor/motivo — el CHECK ck_inventory_movements_adjustment_source_type (migración
# 20260728_0001) ya lo exige a nivel DB para movement_type='adjustment'.
SOURCE_RECEIPT = "receipt"                          # remito de proveedor
SOURCE_RECONCILIATION = "reconciliation"            # reparación/reconciliación de datos
# F-H3.d.4: descuento de una venta IMPORTADA, aplicado cuando el usuario pide el
# replay de esa hoja. Distinto del descuento de una venta en vivo (que no tiene
# source_type ni archivo): sirve para saber, mirando el ledger, qué parte del
# inventario se movió por una decisión sobre historia y cuál por operación diaria.
SOURCE_HISTORICAL_REPLAY = "historical_replay"

SOURCE_TYPES: frozenset[str] = frozenset(
    {
        SOURCE_PURCHASE_IMPORT,
        SOURCE_CATALOG_INITIAL_STOCK,
        SOURCE_MANUAL_ADJUSTMENT,
        SOURCE_RECEIPT,
        SOURCE_RECONCILIATION,
        SOURCE_HISTORICAL_REPLAY,
    }
)

# source_types que representan una COMPRA real de mercadería (cuentan como "comprado"
# y deben tener su COGS). El stock inicial de catálogo ES una compra real.
PURCHASE_SOURCE_TYPES: frozenset[str] = frozenset(
    {SOURCE_PURCHASE_IMPORT, SOURCE_CATALOG_INITIAL_STOCK, SOURCE_RECEIPT}
)

# source_types de un movement_type='adjustment' con procedencia auditada (correcciones
# deliberadas, blindadas por el CHECK ck_inventory_movements_adjustment_source_type).
TAGGED_ADJUSTMENT_SOURCE_TYPES: frozenset[str] = frozenset(
    {SOURCE_RECONCILIATION, SOURCE_MANUAL_ADJUSTMENT}
)

# ── Clasificación de un movimiento para RECONSTRUIR stock ─────────────────────────
# Fuente ÚNICA para que el chequeo AGREGADO (inventory_integrity_service) y el TEMPORAL
# (inventory_temporal_service) interpreten cada movimiento IDÉNTICO. Si divergieran, el
# invariante `ending_balance (temporal) == stock_esperado (agregado)` se rompería y un
# nuevo source_type se contaría distinto en cada chequeo.
MOVEMENT_CLASS_PURCHASE = "purchase"  # + comprado
MOVEMENT_CLASS_ANCHOR = "anchor"  # stock inicial conocido (opening)
MOVEMENT_CLASS_TAGGED_ADJUSTMENT = "tagged_adjustment"  # ± ajuste auditado
MOVEMENT_CLASS_LOSS = "loss"  # + (ya viene negativo en el ledger)
MOVEMENT_CLASS_IGNORE_SALE = "ignore_sale"  # dedup: la venta se cuenta desde sales_entries
MOVEMENT_CLASS_COMPLEX = "complex"  # return / adjustment sin tag → saltear el producto


def classify_stock_movement(movement_type: str, source_type: str | None) -> str:
    """Clasifica un movimiento vivo para la reconstrucción de stock (ver constantes).

    El orden importa: ``purchase`` se evalúa ANTES que el ancla para que una compra con
    ``source_type=catalog_initial_stock`` (el stock inicial de catálogo ES una compra
    real) cuente como compra y no se absorba en el ancla.
    """
    if movement_type == "purchase":
        return MOVEMENT_CLASS_PURCHASE
    if source_type == SOURCE_CATALOG_INITIAL_STOCK:
        return MOVEMENT_CLASS_ANCHOR
    if movement_type == "adjustment" and source_type in TAGGED_ADJUSTMENT_SOURCE_TYPES:
        return MOVEMENT_CLASS_TAGGED_ADJUSTMENT
    if movement_type == "loss":
        return MOVEMENT_CLASS_LOSS
    if movement_type == "sale":
        return MOVEMENT_CLASS_IGNORE_SALE
    return MOVEMENT_CLASS_COMPLEX


def _norm_text(value: object) -> str:
    """Normaliza texto para el hash: sin tildes, minúsculas, trim, espacios colapsados."""
    if value is None:
        return ""
    s = unicodedata.normalize("NFKD", str(value))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.strip().lower().split())


def _norm_num(value: object) -> str:
    """Normaliza un número a string estable (2 decimales); None/'' → ''."""
    if value is None or value == "":
        return ""
    try:
        return f"{Decimal(str(value)):.2f}"
    except (ArithmeticError, ValueError):
        return _norm_text(value)


def ensure_utc(dt: datetime | None) -> datetime | None:
    """Normaliza a UTC ``occurred_at`` (fecha de negocio de un movimiento).

    ``transaction_date`` de ventas/gastos se persiste NAIVE, mientras que
    ``InventoryMovement.occurred_at`` es tz-aware. Un datetime sin tzinfo se asume
    en UTC (no re-interpreta la hora, solo la etiqueta) para poder persistirlo en la
    columna aware sin perder la fecha de negocio original.
    """
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _norm_date(value: object) -> str:
    """Normaliza una fecha a ISO YYYY-MM-DD (solo el día); None → ''."""
    if value is None or value == "":
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return _norm_text(value)


def compute_source_row_hash(
    *,
    product_key: object,
    qty: object,
    unit_cost: object,
    movement_date: object,
    supplier_key: object = None,
    upload_id: UUID | str | None = None,
) -> str:
    """SHA-256 de la identidad lógica de una fila de origen de movimiento.

    Estable ante reordenamiento del Excel (no usa índice de fila). Incluye el
    ``upload_id`` para que la misma fila lógica de DOS archivos distintos NO colisione
    (cada archivo es una lectura propia; la idempotencia intra-archivo la da el reread
    que borra la lectura anterior antes de reimportar).
    """
    parts = [
        _norm_text(product_key),
        _norm_num(qty),
        _norm_num(unit_cost),
        _norm_date(movement_date),
        _norm_text(supplier_key),
        str(upload_id or ""),
    ]
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
