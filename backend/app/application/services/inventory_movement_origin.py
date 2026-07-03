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
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

# ── source_type: valores canónicos ──────────────────────────────────────────────
SOURCE_PURCHASE_IMPORT = "purchase_import"          # libro de compras / gasto de mercadería
SOURCE_CATALOG_INITIAL_STOCK = "catalog_initial_stock"  # stock inicial de un catálogo
SOURCE_MANUAL_ADJUSTMENT = "manual_adjustment"      # ajuste manual de stock
SOURCE_RECEIPT = "receipt"                          # remito de proveedor
SOURCE_RECONCILIATION = "reconciliation"            # reparación/reconciliación de datos

SOURCE_TYPES: frozenset[str] = frozenset(
    {
        SOURCE_PURCHASE_IMPORT,
        SOURCE_CATALOG_INITIAL_STOCK,
        SOURCE_MANUAL_ADJUSTMENT,
        SOURCE_RECEIPT,
        SOURCE_RECONCILIATION,
    }
)

# source_types que representan una COMPRA real de mercadería (cuentan como "comprado"
# y deben tener su COGS). El stock inicial de catálogo ES una compra real.
PURCHASE_SOURCE_TYPES: frozenset[str] = frozenset(
    {SOURCE_PURCHASE_IMPORT, SOURCE_CATALOG_INITIAL_STOCK, SOURCE_RECEIPT}
)


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
