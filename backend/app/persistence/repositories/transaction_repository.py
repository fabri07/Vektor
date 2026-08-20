"""Repository for SaleEntry and ExpenseEntry."""

from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.customer import Customer
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.persistence.repositories._expense_scope import gasto_de_resultado


def _as_date(value: datetime | date | None) -> date | None:
    """Normaliza un timestamp (o date) a `date` para los rangos de la UI."""
    if value is None:
        return None
    return value.date() if isinstance(value, datetime) else value


async def _daily_amount_series(
    session: AsyncSession,
    model: type[SaleEntry] | type[ExpenseEntry],
    tenant_id: UUID,
    from_date: date,
    to_date: date,
) -> list[float]:
    """Serie diaria de montos con zero-fill sobre [from_date, to_date], cronológica.

    Agrupa por `func.date(transaction_date)`, excluye `voided_at` y rellena con 0.0
    los días sin movimiento. `str(row.day)` normaliza la clave entre Postgres
    (devuelve `date`) y SQLite (devuelve `'YYYY-MM-DD'`). Compartido por las series
    de ventas, gastos y caja neta para que el zero-fill y el rango sean idénticos.
    """
    day = func.date(model.transaction_date).label("day")
    q = (
        select(day, func.sum(model.amount).label("total"))
        .where(
            model.tenant_id == tenant_id,
            model.voided_at.is_(None),
            func.date(model.transaction_date) >= from_date,
            func.date(model.transaction_date) <= to_date,
        )
        .group_by(day)
        .order_by(day)
    )
    result = await session.execute(q)
    by_day: dict[str, float] = {str(row.day): float(row.total or 0) for row in result.all()}
    n_days = (to_date - from_date).days + 1
    return [by_day.get(str(from_date + timedelta(days=i)), 0.0) for i in range(n_days)]


async def get_daily_net_series(
    session: AsyncSession,
    tenant_id: UUID,
    days: int = 60,
) -> list[float]:
    """Serie diaria de flujo neto (ventas - gastos) con zero-fill, rango único.

    Calcula `[from_date, to_date]` UNA sola vez y arma ambas series con ese rango,
    de modo que ventas y gastos quedan alineados día a día (evita la desalineación
    si dos `date.today()` separados cruzaran la medianoche).
    """
    to_date = date.today()
    from_date = to_date - timedelta(days=days - 1)
    rev = await _daily_amount_series(session, SaleEntry, tenant_id, from_date, to_date)
    exp = await _daily_amount_series(session, ExpenseEntry, tenant_id, from_date, to_date)
    return [round(r - e, 2) for r, e in zip(rev, exp, strict=True)]


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
        customer_id: UUID | None = None,
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
        if customer_id:
            q = q.where(SaleEntry.customer_id == customer_id)
        q = q.order_by(SaleEntry.transaction_date.desc()).limit(limit).offset(offset)
        result = await self._session.execute(q)
        return list(result.scalars().all())

    async def count_by_tenant(
        self,
        tenant_id: UUID,
        from_date: date | None = None,
        to_date: date | None = None,
        customer_id: UUID | None = None,
    ) -> int:
        """Total que ve `list_by_tenant` — mismos filtros, sin `limit`/`offset`.
        Corrección C4 ampliada (2026-08-19): sin esto el frontend no puede saber
        cuántas páginas hay en total."""
        q = select(func.count(SaleEntry.id)).where(
            SaleEntry.tenant_id == tenant_id,
            SaleEntry.voided_at.is_(None),
        )
        if from_date:
            q = q.where(func.date(SaleEntry.transaction_date) >= from_date)
        if to_date:
            q = q.where(func.date(SaleEntry.transaction_date) <= to_date)
        if customer_id:
            q = q.where(SaleEntry.customer_id == customer_id)
        result = await self._session.execute(q)
        return int(result.scalar_one() or 0)

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

    async def get_daily_revenue_series(
        self,
        tenant_id: UUID,
        days: int = 60,
    ) -> list[float]:
        """Facturación diaria con zero-fill (últimos `days` días, cronológica).

        Serie continua `list[float]` (días sin venta = 0.0) que alimenta
        `stats_enrichment.enrich_timeseries` (promedio diario, tendencia, anomalías).
        """
        to_date = date.today()
        from_date = to_date - timedelta(days=days - 1)
        return await _daily_amount_series(self._session, SaleEntry, tenant_id, from_date, to_date)

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

    async def get_sales_by_customer(
        self,
        tenant_id: UUID,
        from_date: date | None = None,
        to_date: date | None = None,
    ) -> list[dict[str, Any]]:
        """Ventas agregadas por cliente (Fase 2 — análisis de clientes).

        JOIN sales_entries × customers, GROUP BY cliente. Solo ventas con
        `customer_id` no nulo y no anuladas. Por cada cliente: total facturado,
        cantidad de ventas y fecha de la última venta. Ordenado por total desc.
        El cálculo (top N, ticket promedio) lo hace `shared/analytics.py`.

        Returns list[dict]: customer_id, customer_name, total, n_sales, last_sale_date.
        """
        last_sale = func.max(SaleEntry.transaction_date).label("last_sale")
        q = (
            select(
                SaleEntry.customer_id,
                Customer.name.label("customer_name"),
                func.sum(SaleEntry.amount).label("total"),
                func.count(SaleEntry.id).label("n_sales"),
                last_sale,
            )
            .join(Customer, Customer.id == SaleEntry.customer_id)
            .where(
                SaleEntry.tenant_id == tenant_id,
                SaleEntry.voided_at.is_(None),
                SaleEntry.customer_id.isnot(None),
            )
            .group_by(SaleEntry.customer_id, Customer.name)
            .order_by(func.sum(SaleEntry.amount).desc())
        )
        if from_date:
            q = q.where(func.date(SaleEntry.transaction_date) >= from_date)
        if to_date:
            q = q.where(func.date(SaleEntry.transaction_date) <= to_date)
        result = await self._session.execute(q)
        return [
            {
                "customer_id": str(row.customer_id),
                "customer_name": row.customer_name,
                "total": float(row.total or 0),
                "n_sales": int(row.n_sales or 0),
                "last_sale_date": _as_date(row.last_sale),
            }
            for row in result.all()
        ]

    async def get_receivables_by_customer(
        self,
        tenant_id: UUID,
    ) -> list[dict[str, Any]]:
        """Cuentas por cobrar (fiado): ventas con payment_method == 'account'.

        Agrupa por cliente sumando el monto adeudado. Solo ventas no anuladas con
        cliente identificado y método de pago `account` (cuenta corriente / fiado).
        El total general lo computa `shared/analytics.py`.

        Returns list[dict]: customer_id, customer_name, total_owed, n_sales.
        """
        from app.domain.transaction import PaymentMethod  # noqa: PLC0415

        q = (
            select(
                SaleEntry.customer_id,
                Customer.name.label("customer_name"),
                func.sum(SaleEntry.amount).label("total_owed"),
                func.count(SaleEntry.id).label("n_sales"),
            )
            .join(Customer, Customer.id == SaleEntry.customer_id)
            .where(
                SaleEntry.tenant_id == tenant_id,
                SaleEntry.voided_at.is_(None),
                SaleEntry.customer_id.isnot(None),
                SaleEntry.payment_method == PaymentMethod.ACCOUNT.value,
            )
            .group_by(SaleEntry.customer_id, Customer.name)
            .order_by(func.sum(SaleEntry.amount).desc())
        )
        result = await self._session.execute(q)
        return [
            {
                "customer_id": str(row.customer_id),
                "customer_name": row.customer_name,
                "total_owed": float(row.total_owed or 0),
                "n_sales": int(row.n_sales or 0),
            }
            for row in result.all()
        ]

    async def get_balance_by_customer(
        self,
        tenant_id: UUID,
        customer_id: UUID,
    ) -> dict[str, float]:
        """Saldo neto de un cliente: fiado acumulado menos cobros registrados.

        Dos queries simples (account + inflow) para máxima claridad y portabilidad
        entre Postgres y SQLite (tests en memoria).

        Returns dict con claves: total_account, total_paid, balance.
        Sin ventas → todos 0.0 (saldo real de cliente sin movimientos, no None).
        Filtra voided_at IS NULL y tenant_id.
        """
        from app.domain.transaction import PaymentMethod  # noqa: PLC0415

        account_q = select(func.sum(SaleEntry.amount)).where(
            SaleEntry.tenant_id == tenant_id,
            SaleEntry.customer_id == customer_id,
            SaleEntry.payment_method == PaymentMethod.ACCOUNT.value,
            SaleEntry.voided_at.is_(None),
        )
        paid_q = select(func.sum(SaleEntry.amount)).where(
            SaleEntry.tenant_id == tenant_id,
            SaleEntry.customer_id == customer_id,
            SaleEntry.payment_method == "inflow",
            SaleEntry.voided_at.is_(None),
        )
        total_account = float((await self._session.execute(account_q)).scalar() or 0)
        total_paid = float((await self._session.execute(paid_q)).scalar() or 0)
        return {
            "total_account": total_account,
            "total_paid": total_paid,
            "balance": total_account - total_paid,
        }

    async def get_balances_by_customer(
        self,
        tenant_id: UUID,
    ) -> list[dict[str, Any]]:
        """Saldo neto por cada cliente que tiene fiado o cobros.

        Usa agregación condicional (CASE WHEN) en una sola query para obtener
        total_account (payment_method='account') y total_paid (payment_method='inflow')
        en el mismo GROUP BY cliente. Excluye filas con customer_id IS NULL.
        Ordenado por balance (total_account - total_paid) desc — calculado en Python
        para mantener compatibilidad con SQLite.

        Returns list[dict]: customer_id, customer_name, total_account, total_paid,
        balance, n_sales.
        """
        from app.domain.transaction import PaymentMethod  # noqa: PLC0415

        total_account_expr = func.sum(
            case(
                (SaleEntry.payment_method == PaymentMethod.ACCOUNT.value, SaleEntry.amount),
                else_=0,
            )
        ).label("total_account")
        total_paid_expr = func.sum(
            case(
                (SaleEntry.payment_method == "inflow", SaleEntry.amount),
                else_=0,
            )
        ).label("total_paid")

        q = (
            select(
                SaleEntry.customer_id,
                Customer.name.label("customer_name"),
                total_account_expr,
                total_paid_expr,
                func.count(SaleEntry.id).label("n_sales"),
            )
            .join(
                Customer,
                (Customer.id == SaleEntry.customer_id)
                & (Customer.tenant_id == tenant_id),
            )
            .where(
                SaleEntry.tenant_id == tenant_id,
                SaleEntry.voided_at.is_(None),
                SaleEntry.customer_id.isnot(None),
                SaleEntry.payment_method.in_([PaymentMethod.ACCOUNT.value, "inflow"]),
            )
            .group_by(SaleEntry.customer_id, Customer.name)
        )
        result = await self._session.execute(q)
        rows = [
            {
                "customer_id": str(row.customer_id),
                "customer_name": row.customer_name,
                "total_account": float(row.total_account or 0),
                "total_paid": float(row.total_paid or 0),
                "balance": float(row.total_account or 0) - float(row.total_paid or 0),
                "n_sales": int(row.n_sales or 0),
            }
            for row in result.all()
        ]
        return sorted(rows, key=lambda r: r["balance"], reverse=True)

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
        expense_type: str | None = None,
        supplier_id: UUID | None = None,
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
        if expense_type:
            q = q.where(ExpenseEntry.expense_type == expense_type)
        if supplier_id:
            q = q.where(ExpenseEntry.supplier_id == supplier_id)
        q = q.order_by(ExpenseEntry.transaction_date.desc()).limit(limit).offset(offset)
        result = await self._session.execute(q)
        return list(result.scalars().all())

    async def count_by_tenant(
        self,
        tenant_id: UUID,
        from_date: date | None = None,
        to_date: date | None = None,
        category: str | None = None,
        expense_type: str | None = None,
        supplier_id: UUID | None = None,
    ) -> int:
        """Total que ve `list_by_tenant` — mismos filtros, sin `limit`/`offset`.

        Corrección C4 (revisión externa 2026-08-19): el frontend no tenía
        forma de saber cuántas páginas hay en total."""
        q = select(func.count(ExpenseEntry.id)).where(
            ExpenseEntry.tenant_id == tenant_id,
            ExpenseEntry.voided_at.is_(None),
        )
        if from_date:
            q = q.where(func.date(ExpenseEntry.transaction_date) >= from_date)
        if to_date:
            q = q.where(func.date(ExpenseEntry.transaction_date) <= to_date)
        if category:
            q = q.where(ExpenseEntry.category == category)
        if expense_type:
            q = q.where(ExpenseEntry.expense_type == expense_type)
        if supplier_id:
            q = q.where(ExpenseEntry.supplier_id == supplier_id)
        result = await self._session.execute(q)
        return int(result.scalar_one() or 0)

    async def total_expenses(
        self,
        tenant_id: UUID,
        from_date: date | None = None,
        to_date: date | None = None,
    ) -> float:
        q = select(func.sum(ExpenseEntry.amount)).where(
            ExpenseEntry.tenant_id == tenant_id,
            ExpenseEntry.voided_at.is_(None),
            # El flete ya capitalizado en el stock no se cuenta otra vez acá:
            # este total convive con el valor del inventario. Ver `_expense_scope`.
            gasto_de_resultado(ExpenseEntry.custom_fields),
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
        expense_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Gastos agrupados por categoría con totales y porcentaje."""
        q = select(ExpenseEntry.category, func.sum(ExpenseEntry.amount).label("total")).where(
            ExpenseEntry.tenant_id == tenant_id,
            ExpenseEntry.voided_at.is_(None),
            # El flete ya capitalizado en el stock no se cuenta otra vez acá:
            # este total convive con el valor del inventario. Ver `_expense_scope`.
            gasto_de_resultado(ExpenseEntry.custom_fields),
        )
        if from_date:
            q = q.where(func.date(ExpenseEntry.transaction_date) >= from_date)
        if to_date:
            q = q.where(func.date(ExpenseEntry.transaction_date) <= to_date)
        if expense_type:
            q = q.where(ExpenseEntry.expense_type == expense_type)
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

    async def totals_by_expense_type(
        self,
        tenant_id: UUID,
        from_date: date | None = None,
        to_date: date | None = None,
    ) -> dict[str, float]:
        """Totales del período por tipo: {'OPEX': x, 'COGS': y} (0.0 si no hay)."""
        q = select(
            ExpenseEntry.expense_type, func.sum(ExpenseEntry.amount).label("total")
        ).where(
            ExpenseEntry.tenant_id == tenant_id,
            ExpenseEntry.voided_at.is_(None),
            # El flete ya capitalizado en el stock no se cuenta otra vez acá:
            # este total convive con el valor del inventario. Ver `_expense_scope`.
            gasto_de_resultado(ExpenseEntry.custom_fields),
        )
        if from_date:
            q = q.where(func.date(ExpenseEntry.transaction_date) >= from_date)
        if to_date:
            q = q.where(func.date(ExpenseEntry.transaction_date) <= to_date)
        q = q.group_by(ExpenseEntry.expense_type)
        result = await self._session.execute(q)
        totals = {"OPEX": 0.0, "COGS": 0.0}
        for row in result.all():
            totals[str(row.expense_type)] = float(row.total or 0)
        return totals

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

    async def get_daily_expense_series(
        self,
        tenant_id: UUID,
        days: int = 60,
    ) -> list[float]:
        """Gasto diario con zero-fill (últimos `days` días, cronológica).

        Serie continua `list[float]` (días sin gasto = 0.0) que alimenta
        `stats_enrichment.enrich_timeseries`. Espeja `get_daily_revenue_series`.
        """
        to_date = date.today()
        from_date = to_date - timedelta(days=days - 1)
        return await _daily_amount_series(
            self._session, ExpenseEntry, tenant_id, from_date, to_date
        )

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

    async def reclassify(
        self,
        entry: ExpenseEntry,
        *,
        category: str,
        expense_type: str,
        product_id: UUID | None = None,
    ) -> ExpenseEntry:
        """Reclasifica un gasto in-place: category + expense_type (+ product_id opcional).

        Aditivo respecto del resto del repo: muta solo los campos de clasificación
        contable, sin tocar montos ni fechas. ``product_id`` solo se setea cuando
        viene explícito (reventa → producto vendible vinculado); nunca lo limpia.
        """
        entry.category = category
        entry.expense_type = expense_type
        if product_id is not None:
            entry.product_id = product_id
        self._session.add(entry)
        await self._session.flush()
        return entry

    async def delete(self, entry: ExpenseEntry) -> None:
        await self._session.delete(entry)
        await self._session.flush()
