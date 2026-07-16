"""Repository for Customer queries. Always filters by tenant_id."""

from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models._sentinel import SENTINEL_FLAG_KEY
from app.persistence.models.customer import Customer
from app.persistence.models.transaction import SaleEntry
from app.persistence.repositories._jsonb_flags import flag_not_true_sql


def _not_sentinel() -> ColumnElement[bool]:
    """Filtro: excluir el cliente sentinela "Local" de toda métrica de clientes.

    Espejo SQL compartido en ``_jsonb_flags`` (cubre flag string Y booleano
    JSON, que la copia anterior de este filtro no reconocía).
    """
    return flag_not_true_sql(Customer.custom_fields, SENTINEL_FLAG_KEY)


class CustomerRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(
        self,
        customer_id: UUID,
        tenant_id: UUID,
        include_deactivated: bool = False,
    ) -> Customer | None:
        conditions = [
            Customer.id == customer_id,
            Customer.tenant_id == tenant_id,
        ]
        if not include_deactivated:
            conditions.append(Customer.deactivated_at.is_(None))
        result = await self._session.execute(select(Customer).where(*conditions))
        return result.scalar_one_or_none()

    async def get_sentinel_id(self, tenant_id: UUID) -> UUID | None:
        """Read-only: id del centinela "Local" del tenant si existe (no lo crea).

        Lo usan las métricas de clientes (p. ej. la concentración/dependencia) para
        EXCLUIR las ventas anónimas/walk-in del análisis — si no, el centinela
        domina y falsea la "dependencia de pocos clientes". Identifica por el flag
        `_sentinel`, nunca por nombre.
        """
        result = await self._session.execute(
            select(Customer.id).where(
                Customer.tenant_id == tenant_id,
                Customer.deactivated_at.is_(None),
                func.coalesce(Customer.custom_fields[SENTINEL_FLAG_KEY].as_string(), "")
                == "true",
            )
        )
        return result.scalars().first()

    async def find_by_name(
        self,
        name_normalized: str,
        tenant_id: UUID,
    ) -> Customer | None:
        """Busca un cliente activo del tenant por nombre normalizado (lower+trim).

        Excluye el centinela "Local". Devuelve el match exacto o None (sin fuzzy:
        el llamador decide qué hacer ante ambigüedad). Usado por el vínculo
        cobro→cliente (Fase 2) para resolver el nombre que extrae el CEO a un id.
        """
        result = await self._session.execute(
            select(Customer)
            .where(
                func.lower(func.trim(Customer.name)) == name_normalized,
                Customer.tenant_id == tenant_id,
                Customer.deactivated_at.is_(None),
                _not_sentinel(),
            )
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def list_by_tenant(
        self,
        tenant_id: UUID,
        limit: int = 50,
        offset: int = 0,
        include_sentinel: bool = False,
        include_inactive: bool = False,
    ) -> list[Customer]:
        """Lista clientes del tenant. Por defecto excluye el centinela "Local".

        ``include_sentinel=True`` LISTA el centinela si ya existe (es solo un
        SELECT: nunca lo crea — eso pasa únicamente al asignar una venta huérfana).
        ``include_inactive=True`` incluye los dados de baja (``deactivated_at``)
        para mostrarlos en rojo en la UI. Las métricas
        (``count_active``/``get_inactive_customers``) siguen excluyéndolos siempre.
        """
        conditions: list[Any] = [Customer.tenant_id == tenant_id]
        if not include_inactive:
            conditions.append(Customer.deactivated_at.is_(None))
        if not include_sentinel:
            conditions.append(_not_sentinel())
        q = (
            select(Customer)
            .where(*conditions)
            .order_by(Customer.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(q)
        return list(result.scalars().all())

    async def get_inactive_customers(
        self,
        tenant_id: UUID,
        days: int = 60,
    ) -> list[dict[str, Any]]:
        """Clientes activos sin ventas (no anuladas) en los últimos `days` días.

        Subquery: última venta no anulada por cliente. Un cliente activo es inactivo
        si su última venta es anterior al corte, o si nunca tuvo ventas. Solo clientes
        no desactivados. Ordenados por última venta más antigua primero (None primero:
        los que nunca compraron). El ranking lo arma `shared/analytics.py`.

        Returns list[dict]: customer_id, customer_name, last_sale_date (date | None).
        """
        cutoff = date.today() - timedelta(days=days)
        last_sale_sq = (
            select(
                SaleEntry.customer_id.label("customer_id"),
                func.max(SaleEntry.transaction_date).label("last_sale"),
            )
            .where(
                SaleEntry.tenant_id == tenant_id,
                SaleEntry.voided_at.is_(None),
                SaleEntry.customer_id.isnot(None),
            )
            .group_by(SaleEntry.customer_id)
            .subquery()
        )
        q = (
            select(
                Customer.id,
                Customer.name,
                last_sale_sq.c.last_sale,
            )
            .outerjoin(last_sale_sq, last_sale_sq.c.customer_id == Customer.id)
            .where(
                Customer.tenant_id == tenant_id,
                Customer.deactivated_at.is_(None),
                _not_sentinel(),
            )
        )
        result = await self._session.execute(q)
        out: list[dict[str, Any]] = []
        for row in result.all():
            last_sale = row.last_sale
            last_date: date | None = (
                last_sale.date() if isinstance(last_sale, datetime) else last_sale
            )
            if last_date is not None and last_date >= cutoff:
                continue  # compró recientemente → activo, no inactivo
            out.append(
                {
                    "customer_id": str(row.id),
                    "customer_name": row.name,
                    "last_sale_date": last_date,
                }
            )
        # None (nunca compró) primero, luego por fecha ascendente (más viejo arriba).
        out.sort(key=lambda c: (c["last_sale_date"] is not None, c["last_sale_date"] or date.min))
        return out

    async def list_for_dedup(self, tenant_id: UUID) -> list[Customer]:
        """Clientes activos no-sentinela del tenant — base de la deduplicación.

        Devuelve los registros completos para que el import masivo arme el índice por
        documento (DNI/CUIT) en memoria y matchee filas entrantes. Excluye el sentinela
        "Local" (nunca se actualiza por import) y los soft-deleted.
        """
        q = select(Customer).where(
            Customer.tenant_id == tenant_id,
            Customer.deactivated_at.is_(None),
            _not_sentinel(),
        )
        result = await self._session.execute(q)
        return list(result.scalars().all())

    async def count_active(self, tenant_id: UUID) -> int:
        """Cantidad de clientes activos (no desactivados) del tenant."""
        q = select(func.count(Customer.id)).where(
            Customer.tenant_id == tenant_id,
            Customer.deactivated_at.is_(None),
            _not_sentinel(),
        )
        result = await self._session.execute(q)
        return int(result.scalar_one() or 0)

    async def count_sales(self, customer_id: UUID, tenant_id: UUID) -> int:
        """Cantidad de ventas no anuladas asociadas al cliente (historial)."""
        q = select(func.count(SaleEntry.id)).where(
            SaleEntry.customer_id == customer_id,
            SaleEntry.tenant_id == tenant_id,
            SaleEntry.voided_at.is_(None),
        )
        result = await self._session.execute(q)
        return int(result.scalar_one() or 0)

    async def save(self, customer: Customer) -> Customer:
        self._session.add(customer)
        await self._session.flush()
        return customer

    async def soft_delete(self, customer: Customer) -> Customer:
        """Marca el cliente como desactivado (soft-delete). No borra la fila."""
        customer.deactivated_at = datetime.now(UTC)
        self._session.add(customer)
        await self._session.flush()
        return customer

    async def reactivate(self, customer: Customer) -> Customer:
        """Revierte la baja: limpia ``deactivated_at``."""
        customer.deactivated_at = None
        self._session.add(customer)
        await self._session.flush()
        return customer
