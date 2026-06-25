"""Repository for inventory movement queries. Always filters by tenant_id."""

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.supplier import SENTINEL_FLAG_KEY, Supplier, is_sentinel_value

# Compras sin proveedor (supplier_id NULL) y el proveedor sentinela se unifican en una
# sola etiqueta "No identificado" (coincide con el nombre del sentinela) para no mostrar
# dos entradas separadas que son el mismo concepto.
_UNASSIGNED_SUPPLIER_LABEL = "No identificado"
_NO_BRAND_LABEL = "Sin marca"


@dataclass(frozen=True)
class SupplierProductPurchase:
    """Una fila de la tabla "productos comprados a un proveedor"."""

    product_id: UUID
    name: str
    last_purchase_at: datetime | None
    total_qty: float
    unit_price: Decimal


@dataclass(frozen=True)
class SupplierBreakdownProduct:
    """Producto dentro del desglose de un proveedor (importe histórico)."""

    product_id: UUID
    name: str
    brand: str
    total_amount: float
    total_qty: float


@dataclass(frozen=True)
class SupplierPurchaseBreakdown:
    """Compras de mercadería agrupadas por proveedor real (no por texto libre)."""

    supplier_id: UUID | None
    supplier_name: str
    is_unassigned: bool
    total_purchased: float
    # % de movimientos de compra del proveedor con costo unitario conocido.
    coverage_pct: float
    products: list[SupplierBreakdownProduct] = field(default_factory=list)


class InventoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def products_purchased_from_supplier(
        self, tenant_id: UUID, supplier_id: UUID
    ) -> list[SupplierProductPurchase]:
        """Productos comprados a un proveedor, agregados por producto.

        Fuente: ``inventory_movements`` con ``movement_type='purchase'`` y
        ``supplier_id`` del proveedor. Por producto:
          - ``total_qty`` = ``SUM(qty)`` (suma de unidades compradas);
          - ``last_purchase_at`` = ``MAX(created_at)`` (última compra);
          - ``unit_price`` = ``unit_cost`` del movimiento MÁS RECIENTE del producto.

        SIEMPRE filtra por ``tenant_id`` (aislamiento multi-tenant). Orden por
        última compra descendente.
        """
        # Agregados por producto (cantidad total + fecha de última compra).
        agg = (
            select(
                InventoryMovement.product_id.label("product_id"),
                func.coalesce(func.sum(InventoryMovement.qty), 0).label("total_qty"),
                func.max(InventoryMovement.created_at).label("last_purchase_at"),
            )
            .where(
                InventoryMovement.tenant_id == tenant_id,
                InventoryMovement.supplier_id == supplier_id,
                InventoryMovement.movement_type == "purchase",
            )
            .group_by(InventoryMovement.product_id)
            .subquery()
        )

        # Costo unitario del movimiento más reciente por producto: se busca el
        # unit_cost del movimiento cuyo created_at == MAX(created_at) del producto.
        # (Si hay empate de timestamp, gana cualquiera — caso borde improbable.)
        latest_cost = (
            select(
                InventoryMovement.product_id.label("product_id"),
                InventoryMovement.unit_cost.label("unit_cost"),
                func.row_number()
                .over(
                    partition_by=InventoryMovement.product_id,
                    order_by=InventoryMovement.created_at.desc(),
                )
                .label("rn"),
            )
            .where(
                InventoryMovement.tenant_id == tenant_id,
                InventoryMovement.supplier_id == supplier_id,
                InventoryMovement.movement_type == "purchase",
            )
            .subquery()
        )

        q = (
            select(
                agg.c.product_id,
                Product.name,
                agg.c.last_purchase_at,
                agg.c.total_qty,
                latest_cost.c.unit_cost,
            )
            .join(Product, Product.id == agg.c.product_id)
            .join(
                latest_cost,
                (latest_cost.c.product_id == agg.c.product_id) & (latest_cost.c.rn == 1),
            )
            .where(Product.tenant_id == tenant_id)
            .order_by(agg.c.last_purchase_at.desc().nullslast())
        )
        result = await self._session.execute(q)
        rows = result.all()
        return [
            SupplierProductPurchase(
                product_id=row.product_id,
                name=row.name,
                last_purchase_at=row.last_purchase_at,
                total_qty=float(row.total_qty or 0),
                unit_price=row.unit_cost if row.unit_cost is not None else Decimal("0"),
            )
            for row in rows
        ]

    async def suppliers_purchase_breakdown(
        self,
        tenant_id: UUID,
        from_date: date | None = None,
        to_date: date | None = None,
        top_products_per_supplier: int = 10,
        top_suppliers: int = 8,
    ) -> list[SupplierPurchaseBreakdown]:
        """Compras de mercadería agrupadas por proveedor REAL (vía supplier_id).

        Reemplaza el agrupamiento por ``ExpenseEntry.supplier_name`` (texto libre,
        que mostraba marcas). Reglas (feature contable):
          - Importe = ``SUM(qty * unit_cost)`` HISTÓRICO del movimiento, no el costo
            actual del producto. Filas con ``unit_cost`` NULL no suman (SUM ignora NULL).
          - ``coverage_pct`` = % de movimientos de compra del proveedor con costo
            conocido (``unit_cost`` NO NULL y > 0), para señalar cifras calculadas
            sobre datos incompletos. Un costo 0 (placeholder de import) NO cuenta
            como "con costo": de lo contrario coverage diría 100% mientras el total
            está incompleto.
          - ``supplier_id`` NULL → "Sin proveedor asignado" (puede ser proveedor
            eliminado o compra sin proveedor). La sentinela "No identificado" es un
            Supplier real y se muestra con su nombre. No se fusionan.
          - Solo ``movement_type='purchase'`` (compras de mercadería), NO todos los
            egresos del proveedor.
          - Marca desde ``Product.custom_fields['marca']`` → "Sin marca" si falta.
          - Orden estable: proveedores por importe desc, desempate por nombre;
            productos por importe desc, desempate por nombre. Top N productos y
            top ``top_suppliers`` proveedores (es un "Top", no la lista completa).
        """
        amount = func.sum(InventoryMovement.qty * InventoryMovement.unit_cost)
        qty_sum = func.sum(InventoryMovement.qty)
        costed = func.sum(
            case(
                (
                    InventoryMovement.unit_cost.isnot(None)
                    & (InventoryMovement.unit_cost > 0),
                    1,
                ),
                else_=0,
            )
        )
        total_rows = func.count(InventoryMovement.id)

        def _scoped(stmt: Any) -> Any:
            stmt = stmt.where(
                InventoryMovement.tenant_id == tenant_id,
                InventoryMovement.movement_type == "purchase",
            )
            if from_date:
                stmt = stmt.where(func.date(InventoryMovement.created_at) >= from_date)
            if to_date:
                stmt = stmt.where(func.date(InventoryMovement.created_at) <= to_date)
            return stmt

        # Nivel proveedor: total + cobertura de costo.
        supplier_q = _scoped(
            select(
                InventoryMovement.supplier_id.label("supplier_id"),
                amount.label("total_amount"),
                costed.label("n_costed"),
                total_rows.label("n_total"),
            )
        ).group_by(InventoryMovement.supplier_id)
        supplier_rows = (await self._session.execute(supplier_q)).all()
        if not supplier_rows:
            return []

        # Nivel proveedor+producto (para la lista expandible).
        product_q = (
            _scoped(
                select(
                    InventoryMovement.supplier_id.label("supplier_id"),
                    InventoryMovement.product_id.label("product_id"),
                    Product.name.label("name"),
                    Product.custom_fields.label("custom_fields"),
                    amount.label("total_amount"),
                    qty_sum.label("total_qty"),
                )
            )
            .join(
                Product,
                (Product.id == InventoryMovement.product_id)
                & (Product.tenant_id == tenant_id),
            )
            .group_by(
                InventoryMovement.supplier_id,
                InventoryMovement.product_id,
                Product.name,
                Product.custom_fields,
            )
        )
        product_rows = (await self._session.execute(product_q)).all()

        # Nombres + detección de sentinela (los supplier_id no nulos del set).
        supplier_ids = [r.supplier_id for r in supplier_rows if r.supplier_id is not None]
        name_by_id: dict[UUID, str] = {}
        sentinel_ids: set[UUID] = set()
        if supplier_ids:
            name_rows = (
                await self._session.execute(
                    select(Supplier.id, Supplier.name, Supplier.custom_fields).where(
                        Supplier.tenant_id == tenant_id,
                        Supplier.id.in_(supplier_ids),
                    )
                )
            ).all()
            for row in name_rows:
                name_by_id[row.id] = row.name
                if is_sentinel_value((row.custom_fields or {}).get(SENTINEL_FLAG_KEY)):
                    sentinel_ids.add(row.id)

        # Clave canónica: NULL y sentinela → None (un único "No identificado").
        def _canon(sid: UUID | None) -> UUID | None:
            return None if (sid is None or sid in sentinel_ids) else sid

        # Productos por proveedor canónico, fusionando por product_id (NULL + sentinela
        # del mismo producto se suman en una sola fila).
        prod_acc: dict[UUID | None, dict[UUID, dict[str, Any]]] = {}
        for r in product_rows:
            key = _canon(r.supplier_id)
            bucket = prod_acc.setdefault(key, {})
            brand = str((r.custom_fields or {}).get("marca") or "").strip() or _NO_BRAND_LABEL
            cur = bucket.get(r.product_id)
            line_amount = float(r.total_amount or 0)
            line_qty = float(r.total_qty or 0)
            if cur is None:
                bucket[r.product_id] = {
                    "name": r.name,
                    "brand": brand,
                    "total_amount": line_amount,
                    "total_qty": line_qty,
                }
            else:
                cur["total_amount"] += line_amount
                cur["total_qty"] += line_qty

        products_by_supplier: dict[UUID | None, list[SupplierBreakdownProduct]] = {
            key: [
                SupplierBreakdownProduct(
                    product_id=pid,
                    name=p["name"],
                    brand=p["brand"],
                    total_amount=p["total_amount"],
                    total_qty=p["total_qty"],
                )
                for pid, p in bucket.items()
            ]
            for key, bucket in prod_acc.items()
        }

        # Totales por proveedor canónico (fusiona NULL + sentinela).
        supplier_acc: dict[UUID | None, dict[str, float]] = {}
        for r in supplier_rows:
            key = _canon(r.supplier_id)
            a = supplier_acc.setdefault(key, {"total": 0.0, "n_costed": 0.0, "n_total": 0.0})
            a["total"] += float(r.total_amount or 0)
            a["n_costed"] += int(r.n_costed or 0)
            a["n_total"] += int(r.n_total or 0)

        breakdown: list[SupplierPurchaseBreakdown] = []
        for key, a in supplier_acc.items():
            products = sorted(
                products_by_supplier.get(key, []),
                key=lambda p: (-p.total_amount, p.name.lower()),
            )[:top_products_per_supplier]
            n_total = int(a["n_total"])
            coverage = round(a["n_costed"] / n_total * 100, 1) if n_total > 0 else 0.0
            name = _UNASSIGNED_SUPPLIER_LABEL if key is None else name_by_id.get(
                key, _UNASSIGNED_SUPPLIER_LABEL
            )
            breakdown.append(
                SupplierPurchaseBreakdown(
                    supplier_id=key,
                    supplier_name=name,
                    is_unassigned=key is None,
                    total_purchased=a["total"],
                    coverage_pct=coverage,
                    products=products,
                )
            )

        breakdown.sort(key=lambda s: (-s.total_purchased, s.supplier_name.lower()))
        return breakdown[:top_suppliers]
