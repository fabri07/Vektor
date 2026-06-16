"""
Mapa de precios por modelo LLM y estimación de costo en USD.

Pure domain — sin DB, sin LLM. Costos calculados en Decimal (dinero).

⚠️⚠️⚠️ VERIFICAR/ACTUALIZAR con precios oficiales de Anthropic ⚠️⚠️⚠️
Los valores de abajo son estimaciones/placeholders por 1M de tokens (input/output).
Antes de exponer estos números a usuarios o facturación, confirmar contra
la página de pricing oficial de Anthropic. Algunos modelos (p. ej. claude-fable-5)
usan valores placeholder.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

# Precio por 1.000.000 de tokens, en USD.
# ⚠️ VERIFICAR/ACTUALIZAR con precios oficiales de Anthropic ⚠️
MODEL_PRICING: dict[str, dict[str, Decimal]] = {
    # Opus
    "claude-opus-4-8": {"input": Decimal("15"), "output": Decimal("75")},
    # Sonnet (actual + histórico)
    "claude-sonnet-4-6": {"input": Decimal("3"), "output": Decimal("15")},
    "claude-sonnet-4-5": {"input": Decimal("3"), "output": Decimal("15")},
    # Haiku (histórico)
    "claude-haiku-4-5-20251001": {"input": Decimal("1"), "output": Decimal("5")},
    "claude-haiku-4-5": {"input": Decimal("1"), "output": Decimal("5")},
    # Fable (placeholder — ⚠️ confirmar precios oficiales)
    "claude-fable-5": {"input": Decimal("3"), "output": Decimal("15")},
}

_PER_MILLION = Decimal("1000000")
_QUANT = Decimal("0.000001")  # 6 decimales


def estimate_cost_usd(
    model: str, input_tokens: int, output_tokens: int
) -> tuple[Decimal, bool]:
    """
    Estima el costo en USD de una llamada LLM.

    Devuelve ``(costo, priced)`` donde ``priced=False`` si el modelo no está en
    ``MODEL_PRICING`` (en ese caso el costo es 0 — no inventamos precios).

    Costo = input/1_000_000 * price_in + output/1_000_000 * price_out,
    cuantizado a 6 decimales (ROUND_HALF_UP).
    """
    pricing = MODEL_PRICING.get(model)
    if pricing is None:
        return (Decimal("0").quantize(_QUANT, rounding=ROUND_HALF_UP), False)

    cost = (
        Decimal(input_tokens) / _PER_MILLION * pricing["input"]
        + Decimal(output_tokens) / _PER_MILLION * pricing["output"]
    )
    return (cost.quantize(_QUANT, rounding=ROUND_HALF_UP), True)
