"""Cálculos financieros determinísticos — sin LLM.

Todas las funciones filtran exclusivamente datos con provenance='REAL'
para evitar mezclar transacciones de demo con datos reales del tenant.

Usar Decimal para todos los cálculos monetarios; nunca float.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.observability.logger import get_logger
from app.persistence.models.transaction import ExpenseEntry, SaleEntry

logger = get_logger(__name__)

PROVENANCE_REAL = "REAL"
PROVENANCE_DEMO = "DEMO"
_WINDOW_DAYS = 30
_TWO_PLACES = Decimal("0.01")


def _today() -> date:
    return date.today()


def _window_start() -> date:
    return _today() - timedelta(days=_WINDOW_DAYS)


async def calcular_flujo_neto_30d(
    tenant_id: uuid.UUID, db: AsyncSession, provenance: str = PROVENANCE_REAL
) -> dict[str, Any]:
    """
    Retorna ventas, gastos y flujo neto de los últimos 30 días.
    Por defecto solo incluye datos provenance='REAL'.
    """
    desde = _window_start()

    ventas_q = select(func.coalesce(func.sum(SaleEntry.amount), Decimal("0"))).where(
        SaleEntry.tenant_id == tenant_id,
        SaleEntry.provenance == provenance,
        SaleEntry.voided_at.is_(None),
        SaleEntry.transaction_date >= desde,
    )
    gastos_q = select(func.coalesce(func.sum(ExpenseEntry.amount), Decimal("0"))).where(
        ExpenseEntry.tenant_id == tenant_id,
        ExpenseEntry.provenance == provenance,
        ExpenseEntry.transaction_date >= desde,
    )

    total_ventas = Decimal(str((await db.scalar(ventas_q)) or 0))
    total_gastos = Decimal(str((await db.scalar(gastos_q)) or 0))
    flujo_neto = total_ventas - total_gastos

    return {
        "total_ventas": total_ventas,
        "total_gastos": total_gastos,
        "flujo_neto": flujo_neto,
        "periodo_dias": _WINDOW_DAYS,
        "desde": desde.isoformat(),
        "hasta": _today().isoformat(),
    }


async def calcular_margen_bruto(
    tenant_id: uuid.UUID, db: AsyncSession, provenance: str = PROVENANCE_REAL
) -> dict[str, Any]:
    """
    Retorna margen bruto en % y absoluto sobre los últimos 30 días.
    Si ventas == 0, retorna {"margen_pct": None, "sin_datos": True}.
    """
    flujo = await calcular_flujo_neto_30d(tenant_id, db, provenance=provenance)
    ventas = flujo["total_ventas"]
    gastos = flujo["total_gastos"]

    if ventas == Decimal("0"):
        return {"margen_pct": None, "margen_abs": None, "sin_datos": True}

    margen_abs = ventas - gastos
    margen_pct = (margen_abs / ventas * Decimal("100")).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)

    return {
        "margen_pct": margen_pct,
        "margen_abs": margen_abs,
        "base_calculo": ventas,
        "sin_datos": False,
    }


async def calcular_ticket_promedio(
    tenant_id: uuid.UUID, db: AsyncSession, provenance: str = PROVENANCE_REAL
) -> dict[str, Any]:
    """
    Retorna ticket promedio y número de transacciones de los últimos 30 días.
    """
    desde = _window_start()

    count_q = select(func.count(SaleEntry.id)).where(
        SaleEntry.tenant_id == tenant_id,
        SaleEntry.provenance == provenance,
        SaleEntry.voided_at.is_(None),
        SaleEntry.transaction_date >= desde,
    )
    sum_q = select(func.coalesce(func.sum(SaleEntry.amount), Decimal("0"))).where(
        SaleEntry.tenant_id == tenant_id,
        SaleEntry.provenance == provenance,
        SaleEntry.voided_at.is_(None),
        SaleEntry.transaction_date >= desde,
    )

    n_transacciones = (await db.scalar(count_q)) or 0
    total = Decimal(str((await db.scalar(sum_q)) or 0))

    if n_transacciones == 0:
        return {"ticket_promedio": None, "n_transacciones": 0, "sin_datos": True}

    ticket = (total / Decimal(str(n_transacciones))).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
    return {
        "ticket_promedio": ticket,
        "n_transacciones": n_transacciones,
        "sin_datos": False,
    }


async def calcular_rotacion_inventario(
    tenant_id: uuid.UUID, db: AsyncSession, provenance: str = PROVENANCE_REAL
) -> dict[str, Any]:
    """
    Retorna días de rotación estimados y ventas diarias promedio.
    Requiere ventas > 0 para calcular la rotación.
    """
    from app.persistence.models.product import Product  # noqa: PLC0415

    desde = _window_start()

    stock_q = select(func.coalesce(func.sum(Product.stock_units), 0)).where(
        Product.tenant_id == tenant_id,
        Product.provenance == provenance,
        Product.is_active.is_(True),
    )
    ventas_q = select(func.coalesce(func.sum(SaleEntry.amount), Decimal("0"))).where(
        SaleEntry.tenant_id == tenant_id,
        SaleEntry.provenance == provenance,
        SaleEntry.voided_at.is_(None),
        SaleEntry.transaction_date >= desde,
    )

    stock_units = int((await db.scalar(stock_q)) or 0)
    ventas_30d = Decimal(str((await db.scalar(ventas_q)) or 0))

    if ventas_30d == Decimal("0"):
        return {"dias_rotacion": None, "ventas_diarias_promedio": None, "sin_datos": True}

    ventas_diarias = (ventas_30d / Decimal("30")).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
    return {
        "valor_stock_unidades": stock_units,
        "ventas_diarias_promedio": ventas_diarias,
        "sin_datos": False,
    }


async def get_financial_summary(
    tenant_id: uuid.UUID, db: AsyncSession, provenance: str = PROVENANCE_REAL
) -> dict[str, Any]:
    """
    Compone todos los cálculos financieros en un dict único.
    Si TODOS los valores tienen sin_datos=True, retorna {"estado": "SIN_DATOS"}.

    Este dict se inyecta en el contexto del agente como datos ya calculados.
    El LLM NUNCA calcula — solo narra estos valores.
    """
    try:
        flujo = await calcular_flujo_neto_30d(tenant_id, db, provenance=provenance)
        margen = await calcular_margen_bruto(tenant_id, db, provenance=provenance)
        ticket = await calcular_ticket_promedio(tenant_id, db, provenance=provenance)
        rotacion = await calcular_rotacion_inventario(tenant_id, db, provenance=provenance)
    except Exception as exc:
        logger.warning(
            "deterministic_finance.error",
            tenant_id=str(tenant_id),
            error=str(exc),
        )
        return {"estado": "SIN_DATOS", "mensaje": "error calculando datos financieros"}

    sin_datos_total = (
        flujo["total_ventas"] == Decimal("0")
        and flujo["total_gastos"] == Decimal("0")
        and margen.get("sin_datos", True)
        and ticket.get("sin_datos", True)
    )

    if sin_datos_total:
        return {
            "estado": "SIN_DATOS",
            "mensaje": "sin datos reales cargados",
            "provenance_checked": provenance,
        }

    return {
        "estado": "OK",
        "flujo_neto_30d": flujo,
        "margen": margen,
        "ticket_promedio": ticket,
        "rotacion": rotacion,
        "provenance": provenance,
    }
