"""Repository for SaleEntry and ExpenseEntry."""

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.transaction import ExpenseEntry, SaleEntry


def _as_date(value: datetime | date | None) -> date | None:
    """Normaliza un timestamp (o date) a `date` para los rangos de la UI."""
    if value is None:
        return None
    return value.date() if isinstance(value, datetime) else value


class SaleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, sale_id: UUID, tenant_id: UUID) -> SaleEntry | None:
        result = await self._session.execute(
            select(SaleEntry).where(
                SaleEntry.id == sale_id,
                SaleEntry.tenant_id == tenant_id,
                SaleEntry.voided_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def list_by_tenant(
        self,
        tenant_id: UUID,
        from_date: date | None = None,
        to_date: date | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SaleEntry]:
        q = select(SaleEntry).where(
            SaleEntry.tenant_id == tenant_id,
            SaleEntry.voided_at.is_(None),
        )
        if from_date:
            q = q.where(func.date(SaleEntry.transaction_date) >= from_date)
        if to_date:
            q = q.where(func.date(SaleEntry.transaction_date) <= to_date)
        q = q.order_by(SaleEntry.transaction_date.desc()).limit(limit).offset(offset)
        result = await self._session.execute(q)
        return list(result.scalars().all())

    async def total_revenue(
        self,
        tenant_id: UUID,
        from_date: date | None = None,
        to_date: date | None = None,
    ) -> float:
        q = select(func.sum(SaleEntry.amount)).where(
            SaleEntry.tenant_id == tenant_id,
            SaleEntry.voided_at.is_(None),
        )
        if from_date:
            q = q.where(func.date(SaleEntry.transaction_date) >= from_date)
        if to_date:
            q = q.where(func.date(SaleEntry.transaction_date) <= to_date)
        result = await self._session.execute(q)
        return float(result.scalar_one() or 0)

    async def count_by_date_range(
        self,
        tenant_id: UUID,
        from_date: date,
        to_date: date,
    ) -> int:
        q = select(func.count(SaleEntry.id)).where(
            SaleEntry.tenant_id == tenant_id,
            SaleEntry.voided_at.is_(None),
            func.date(SaleEntry.transaction_date) >= from_date,
            func.date(SaleEntry.transaction_date) <= to_date,
        )
        result = await self._session.execute(q)
        return int(result.scalar_one() or 0)

    async def cash_breakdown_by_method(
        self,
        tenant_id: UUID,
        from_date: date,
        to_date: date,
    ) -> list[dict[str, Any]]:
        """Ventas diarias agrupadas por método de pago."""
        day = func.date(SaleEntry.transaction_date).label("day")
        q = (
            select(
                day,
                SaleEntry.payment_method,
                func.sum(SaleEntry.amount).label("total"),
            )
            .where(
                SaleEntry.tenant_id == tenant_id,
                SaleEntry.voided_at.is_(None),
                func.date(SaleEntry.transaction_date) >= from_date,
                func.date(SaleEntry.transaction_date) <= to_date,
            )
            .group_by(day, SaleEntry.payment_method)
            .order_by(day)
        )
        result = await self._session.execute(q)
        return [
            {
                "date": str(row.day),
                "payment_method": row.payment_method or "OTROS",
                "total": float(row.total or 0),
            }
            for row in result.all()
        ]

    async def get_daily_velocity(
        self,
        tenant_id: UUID,
        days: int = 30,
    ) -> dict[str, float]:
        """Velocidad de venta diaria por producto (unidades/día) en una ventana (Sprint 17).

        Returns dict[product_id_str, float]: unidades vendidas / `days`. Solo incluye
        ventas con `product_id` no nulo y no anuladas. Se usa para días de stock,
        sobrestock y priorización de reposición.
        """
        from datetime import date as _date  # noqa: PLC0415
        from datetime import timedelta as _timedelta

        cutoff = _date.today() - _timedelta(days=days)
        q = (
            select(
                SaleEntry.product_id,
                func.sum(SaleEntry.quantity).label("units"),
            )
            .where(
                SaleEntry.tenant_id == tenant_id,
                SaleEntry.voided_at.is_(None),
                SaleEntry.product_id.isnot(None),
                SaleEntry.transaction_date >= cutoff,
            )
            .group_by(SaleEntry.product_id)
        )
        result = await self._session.execute(q)
        span = days if days > 0 else 1
        return {str(row.product_id): round(float(row.units or 0) / span, 4) for row in result.all()}

    async def get_sales_by_product(
        self,
        tenant_id: UUID,
        from_date: date,
        to_date: date,
    ) -> list[dict[str, Any]]:
        """Ventas agregadas por producto en un período (Sprint 17).

        Agrupa por product_id sumando unidades y facturación. Solo ventas con
        product_id no nulo y no anuladas. El cruce con costo/margen lo hace el agente.
        Returns list[dict]: product_id, units, revenue, n_sales.
        """
        q = (
            select(
                SaleEntry.product_id,
                func.sum(SaleEntry.quantity).label("units"),
                func.sum(SaleEntry.amount).label("revenue"),
                func.count(SaleEntry.id).label("n_sales"),
            )
            .where(
                SaleEntry.tenant_id == tenant_id,
                SaleEntry.voided_at.is_(None),
                SaleEntry.product_id.isnot(None),
                func.date(SaleEntry.transaction_date) >= from_date,
                func.date(SaleEntry.transaction_date) <= to_date,
            )
            .group_by(SaleEntry.product_id)
            .order_by(func.sum(SaleEntry.amount).desc())
        )
        result = await self._session.execute(q)
        return [
            {
                "product_id": str(row.product_id),
                "units": int(row.units or 0),
                "revenue": float(row.revenue or 0),
                "n_sales": int(row.n_sales or 0),
            }
            for row in result.all()
        ]

    async def date_range(self, tenant_id: UUID) -> tuple[date | None, date | None]:
        """Fecha de la primera y última venta no anulada del tenant.

        Devuelve `(None, None)` si el tenant no tiene ventas — un negocio sin datos
        no tiene "rango cero", tiene ausencia de rango (la UI lo trata como
        "solo presets, sin navegación de años"). Nunca coercionar con `or`.
        """
        q = select(
            func.min(SaleEntry.transaction_date),
            func.max(SaleEntry.transaction_date),
        ).where(
            SaleEntry.tenant_id == tenant_id,
            SaleEntry.voided_at.is_(None),
        )
        row = (await self._session.execute(q)).one()
        return _as_date(row[0]), _as_date(row[1])

    async def save(self, entry: SaleEntry) -> SaleEntry:
        self._session.add(entry)
        await self._session.flush()
        return entry

    async def bulk_save(self, entries: list[SaleEntry]) -> list[SaleEntry]:
        self._session.add_all(entries)
        await self._session.flush()
        return entries

    async def delete(self, entry: SaleEntry) -> None:
        await self._session.delete(entry)
        await self._session.flush()


class ExpenseRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, expense_id: UUID, tenant_id: UUID) -> ExpenseEntry | None:
        result = await self._session.execute(
            select(ExpenseEntry).where(
                ExpenseEntry.id == expense_id,
                ExpenseEntry.tenant_id == tenant_id,
                ExpenseEntry.voided_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def list_by_tenant(
        self,
        tenant_id: UUID,
        from_date: date | None = None,
        to_date: date | None = None,
        category: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ExpenseEntry]:
        q = select(ExpenseEntry).where(
            ExpenseEntry.tenant_id == tenant_id,
            ExpenseEntry.voided_at.is_(None),
        )
        if from_date:
            q = q.where(func.date(ExpenseEntry.transaction_date) >= from_date)
        if to_date:
            q = q.where(func.date(ExpenseEntry.transaction_date) <= to_date)
        if category:
            q = q.where(ExpenseEntry.category == category)
        q = q.order_by(ExpenseEntry.transaction_date.desc()).limit(limit).offset(offset)
        result = await self._session.execute(q)
        return list(result.scalars().all())

    async def total_expenses(
        self,
        tenant_id: UUID,
        from_date: date | None = None,
        to_date: date | None = None,
    ) -> float:
        q = select(func.sum(ExpenseEntry.amount)).where(
            ExpenseEntry.tenant_id == tenant_id,
            ExpenseEntry.voided_at.is_(None),
        )
        if from_date:
            q = q.where(func.date(ExpenseEntry.transaction_date) >= from_date)
        if to_date:
            q = q.where(func.date(ExpenseEntry.transaction_date) <= to_date)
        result = await self._session.execute(q)
        return float(result.scalar_one() or 0)

    async def count_by_date_range(
        self,
        tenant_id: UUID,
        from_date: date,
        to_date: date,
    ) -> int:
        q = select(func.count(ExpenseEntry.id)).where(
            ExpenseEntry.tenant_id == tenant_id,
            ExpenseEntry.voided_at.is_(None),
            func.date(ExpenseEntry.transaction_date) >= from_date,
            func.date(ExpenseEntry.transaction_date) <= to_date,
        )
        result = await self._session.execute(q)
        return int(result.scalar_one() or 0)

    async def expenses_by_category(
        self,
        tenant_id: UUID,
        from_date: date | None = None,
        to_date: date | None = None,
    ) -> list[dict[str, Any]]:
        """Gastos agrupados por categoría con totales y porcentaje."""
        q = select(ExpenseEntry.category, func.sum(ExpenseEntry.amount).label("total")).where(
            ExpenseEntry.tenant_id == tenant_id,
            ExpenseEntry.voided_at.is_(None),
        )
        if from_date:
            q = q.where(func.date(ExpenseEntry.transaction_date) >= from_date)
        if to_date:
            q = q.where(func.date(ExpenseEntry.transaction_date) <= to_date)
        q = q.group_by(ExpenseEntry.category).order_by(func.sum(ExpenseEntry.amount).desc())
        result = await self._session.execute(q)
        rows = result.all()
        grand_total = sum(float(r.total or 0) for r in rows)
        return [
            {
                "category": r.category or "OTHER",
                "total": float(r.total or 0),
                "pct": (
                    round(float(r.total or 0) / grand_total * 100, 1) if grand_total > 0 else 0.0
                ),
            }
            for r in rows
        ]

    async def top_suppliers(
        self,
        tenant_id: UUID,
        from_date: date | None = None,
        to_date: date | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Top proveedores por gasto total."""
        q = select(ExpenseEntry.supplier_name, func.sum(ExpenseEntry.amount).label("total")).where(
            ExpenseEntry.tenant_id == tenant_id,
            ExpenseEntry.voided_at.is_(None),
            ExpenseEntry.supplier_name.isnot(None),
            ExpenseEntry.supplier_name != "",
        )
        if from_date:
            q = q.where(func.date(ExpenseEntry.transaction_date) >= from_date)
        if to_date:
            q = q.where(func.date(ExpenseEntry.transaction_date) <= to_date)
        q = (
            q.group_by(ExpenseEntry.supplier_name)
            .order_by(func.sum(ExpenseEntry.amount).desc())
            .limit(limit)
        )
        result = await self._session.execute(q)
        rows = result.all()

        total_q = select(func.sum(ExpenseEntry.amount)).where(
            ExpenseEntry.tenant_id == tenant_id,
            ExpenseEntry.voided_at.is_(None),
            ExpenseEntry.supplier_name.isnot(None),
            ExpenseEntry.supplier_name != "",
        )
        if from_date:
            total_q = total_q.where(func.date(ExpenseEntry.transaction_date) >= from_date)
        if to_date:
            total_q = total_q.where(func.date(ExpenseEntry.transaction_date) <= to_date)
        total_result = await self._session.execute(total_q)
        grand_total = float(total_result.scalar_one() or 0)

        return [
            {
                "supplier_name": r.supplier_name,
                "total": float(r.total or 0),
                "pct": (
                    round(float(r.total or 0) / grand_total * 100, 1) if grand_total > 0 else 0.0
                ),
            }
            for r in rows
        ]

    async def cash_breakdown_by_method(
        self,
        tenant_id: UUID,
        from_date: date,
        to_date: date,
    ) -> list[dict[str, Any]]:
        """Gastos diarios agrupados por método de pago."""
        day = func.date(ExpenseEntry.transaction_date).label("day")
        q = (
            select(
                day,
                ExpenseEntry.payment_method,
                func.sum(ExpenseEntry.amount).label("total"),
            )
            .where(
                ExpenseEntry.tenant_id == tenant_id,
                ExpenseEntry.voided_at.is_(None),
                func.date(ExpenseEntry.transaction_date) >= from_date,
                func.date(ExpenseEntry.transaction_date) <= to_date,
            )
            .group_by(day, ExpenseEntry.payment_method)
            .order_by(day)
        )
        result = await self._session.execute(q)
        return [
            {
                "date": str(row.day),
                "payment_method": row.payment_method or "OTROS",
                "total": float(row.total or 0),
            }
            for row in result.all()
        ]

    async def get_expense_stats_by_category(
        self,
        tenant_id: UUID,
        days: int = 90,
    ) -> dict[str, dict[str, Any]]:
        """Estadística de gastos por categoría en una ventana (Sprint 17).

        Devuelve, por categoría: count, total, media, desvío estándar muestral y
        el listado de montos. Alimenta la detección de gastos anómalos (media + 2σ)
        y el análisis de costos fijos/variables. Solo gastos no anulados.

        Returns dict[category, {count, total, mean, stddev, amounts: list[float]}].
        """
        from datetime import date as _date  # noqa: PLC0415
        from datetime import timedelta as _timedelta
        from statistics import pstdev  # noqa: PLC0415

        cutoff = _date.today() - _timedelta(days=days)
        q = select(
            ExpenseEntry.category,
            ExpenseEntry.amount,
        ).where(
            ExpenseEntry.tenant_id == tenant_id,
            ExpenseEntry.voided_at.is_(None),
            ExpenseEntry.transaction_date >= cutoff,
        )
        result = await self._session.execute(q)
        by_cat: dict[str, list[float]] = {}
        for row in result.all():
            cat = row.category or "OTHER"
            by_cat.setdefault(cat, []).append(float(row.amount or 0))

        stats: dict[str, dict[str, Any]] = {}
        for cat, amounts in by_cat.items():
            n = len(amounts)
            total = sum(amounts)
            mean = total / n if n else 0.0
            # pstdev (poblacional) — robusto con n pequeño; con n==1 da 0.0
            stddev = pstdev(amounts) if n > 1 else 0.0
            stats[cat] = {
                "count": n,
                "total": round(total, 2),
                "mean": round(mean, 2),
                "stddev": round(stddev, 2),
                "amounts": amounts,
            }
        return stats

    async def date_range(self, tenant_id: UUID) -> tuple[date | None, date | None]:
        """Fecha del primer y último gasto no anulado del tenant.

        Devuelve `(None, None)` si no hay gastos (ausencia de rango, no rango cero).
        """
        q = select(
            func.min(ExpenseEntry.transaction_date),
            func.max(ExpenseEntry.transaction_date),
        ).where(
            ExpenseEntry.tenant_id == tenant_id,
            ExpenseEntry.voided_at.is_(None),
        )
        row = (await self._session.execute(q)).one()
        return _as_date(row[0]), _as_date(row[1])

    async def save(self, entry: ExpenseEntry) -> ExpenseEntry:
        self._session.add(entry)
        await self._session.flush()
        return entry

    async def delete(self, entry: ExpenseEntry) -> None:
        await self._session.delete(entry)
        await self._session.flush()
