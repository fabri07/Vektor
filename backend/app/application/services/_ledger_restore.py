"""Restauración de entidades desde el ``before_json`` del ledger de reparaciones.

Fuente ÚNICA compartida por los dos caminos que revierten un import:

* ``reread_service.undo_reread`` — deshacer una relectura aplicada.
* ``file_deletion_service.revert_file_data`` — borrar un archivo y revertir lo que
  importó.

Antes vivía solo en ``reread_service``, y por eso el borrado de archivo
**ignoraba los ``UPDATE_PRODUCT``**: si un archivo pisaba el precio de un producto
preexistente, ese precio quedaba pisado para siempre aunque el ``before`` estuviera
guardado. Duplicar la lógica en el segundo caller habría sido peor: son las mismas
reglas de tipos, la misma allowlist y las mismas exclusiones, y divergirían.

REGLAS QUE NO SE NEGOCIAN
-------------------------
``stock_units`` NUNCA se restaura por ``setattr``. Su reversa es EXCLUSIVAMENTE el
mecanismo incremental de movimientos de inventario
(``stock_service.void_movement``/``unvoid_movement``). ``stock_units`` no es
Σ(inventory_movements) — tiene base no-ledger de alta manual, chat, seed y catálogo
con stock absoluto —, así que asignarlo desde un snapshot destruiría esa base.

``unit_cost_ars`` / ``list_price_ars`` / ``sale_price_ars`` SÍ se restauran acá: a
diferencia de ``stock_units``, el mecanismo de movimientos no los toca nunca (solo
ajusta stock/``current_qty``), así que sin esto el undo los dejaría permanentemente
en lo que dijo el archivo.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

# Campos editables de un maestro que se serializan al snapshot y se restauran.
MASTER_SNAPSHOT_FIELDS: dict[str, tuple[str, ...]] = {
    "customer": (
        "customer_type", "name", "last_name", "doc_type", "dni", "cuit",
        "iva_condition", "email", "phone", "address", "locality", "province",
        "postal_code", "birthday", "notes", "credit_limit",
    ),
    "supplier": ("name", "last_name", "cuil", "payment_method", "email", "phone", "notes"),
}

# Producto: todo lo mutable que NO sea stock (ver docstring del módulo).
PRODUCT_RESTORE_FIELDS: tuple[str, ...] = (
    "sale_price_ars",
    "list_price_ars",
    "unit_cost_ars",
    "sku",
    "barcode",
    "category",
    "acquired_at",
    "expiry_date",
)

RESTORE_FIELDS: dict[str, tuple[str, ...]] = {
    "customer": MASTER_SNAPSHOT_FIELDS["customer"],
    "supplier": MASTER_SNAPSHOT_FIELDS["supplier"],
    "product": PRODUCT_RESTORE_FIELDS,
}


def coerce_restore_value(field: str, value: Any) -> Any:
    """Deserializa un valor JSON-safe del snapshot al tipo que espera el modelo.

    Los nombres de campo no colisionan entre kinds con tipos distintos:
    ``birthday`` (Customer) y ``expiry_date`` (Product) son ``Date``;
    ``acquired_at`` (Product) es ``DateTime`` — con hora, ``date.fromisoformat`` no
    lo parsea; ``credit_limit`` y los tres precios son ``Decimal``. El resto
    (strings) se devuelve tal cual.
    """
    if value is None:
        return None
    if field in ("birthday", "expiry_date"):
        return date.fromisoformat(value)
    if field == "acquired_at":
        return datetime.fromisoformat(value)
    if field in ("credit_limit", "sale_price_ars", "list_price_ars", "unit_cost_ars"):
        return Decimal(value)
    return value


def restore_from_before(entity: Any, kind: str, before: dict[str, Any]) -> list[str]:
    """Aplica el ``before`` del ledger a la entidad. Devuelve los campos tocados.

    Solo los de la allowlist de ese kind y solo los presentes en el snapshot: un
    campo ausente significa "no se capturó", no "poner en None".
    """
    restaurados: list[str] = []
    for f in RESTORE_FIELDS.get(kind, ()):
        if f not in before:
            continue
        setattr(entity, f, coerce_restore_value(f, before[f]))
        restaurados.append(f)
    return restaurados


def captured_updated_at(after: dict[str, Any] | None) -> str | None:
    """El ``updated_at`` que el ledger capturó al dejar la entidad como quedó."""
    return (after or {}).get("updated_at")


def entity_changed_since_ledger(entity: Any, after: dict[str, Any] | None) -> bool:
    """¿Alguien tocó la entidad DESPUÉS de que el import/relectura la dejó así?

    Compara el ``updated_at`` actual contra el capturado en el ``after``. Es una
    protección de grano grueso: dice que hubo UN cambio, no cuál ni quién.

    **El caller debe refrescar la entidad antes de llamar**: ``updated_at`` tiene
    ``onupdate`` server-side y puede quedar expirado tras flushes previos de la
    misma transacción (mismo patrón MissingGreenlet del resto del servicio).
    """
    actual = entity.updated_at.isoformat() if entity.updated_at else None
    return captured_updated_at(after) != actual
