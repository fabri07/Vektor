"""Product domain constants.

NULL threshold means "not configured by user" → apply DEFAULT_LOW_STOCK_THRESHOLD_UNITS.
Explicit 0 means "only out-of-stock counts as critical" (user chose no low-stock warning).
"""

# Umbral de stock bajo aplicado cuando el tenant no configuró uno manualmente.
# Puede sobreescribirse por vertical o tenant en el futuro.
DEFAULT_LOW_STOCK_THRESHOLD_UNITS: int = 5


def effective_threshold(low_stock_threshold_units: int | None) -> int:
    """Devuelve el umbral efectivo para una unidad de stock.

    None → DEFAULT_LOW_STOCK_THRESHOLD_UNITS (no configurado).
    0   → 0 (umbral explícito: sólo sin-stock cuenta como crítico).
    >0  → valor configurado por el tenant.
    """
    if low_stock_threshold_units is None:
        return DEFAULT_LOW_STOCK_THRESHOLD_UNITS
    return low_stock_threshold_units
