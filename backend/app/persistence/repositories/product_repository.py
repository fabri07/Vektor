"""Repository for Product catalog queries."""

from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

# F3-T3 review (MINOR, no bloqueante): import de Application desde Persistence,
# viola la dirección de capas del repo. Evaluado mover el acquire a cada caller de
# save() (4 call sites: products.py x3, pending_action_service.py x1) pero se
# decidió mantenerlo ACÁ a propósito: save() es el único chokepoint común de TODA
# creación/actualización de producto vía repo, y un futuro caller que se agregue
# sin acordarse del acquire rompería silenciosamente la barrera de exclusión mutua
# contra el dedup. El riesgo de un import cruzado puntual es menor que el riesgo de
# una regresión de correctness no cubierta por tests. Revisar si en algún momento
# `maintenance_lock_service` se muda a un paquete neutral (ni app ni persistence).
from app.application.services import maintenance_lock_service
from app.domain.scan_code import CodigoEscaneado, TipoDeCodigo, variantes_gtin
from app.domain.text_norm import normalize_sku
from app.persistence.models.product import Product


class ProductRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, product_id: UUID, tenant_id: UUID) -> Product | None:
        result = await self._session.execute(
            select(Product).where(
                Product.id == product_id,
                Product.tenant_id == tenant_id,
            )
        )
        return result.scalar_one_or_none()

    async def find_by_scan(
        self, tenant_id: UUID, codigo: CodigoEscaneado
    ) -> list[tuple[Product, str]]:
        """Productos ACTIVOS que responden a un código escaneado, con la columna
        por la que coincidieron. Sin elegir: si hay más de uno, decide el caller.

        Cada tipo busca en SU columna (ver ``domain/scan_code.py``). Además, todos
        los tipos se comparan contra ``sku_normalized`` como texto literal: es la
        forma de detectar que el GTIN de un producto es el SKU opaco de otro. Si
        esa búsqueda no se hiciera, el escaneo elegiría el primero que aparece y
        el caso ambiguo quedaría invisible.
        """
        condiciones: list[tuple[Any, str]] = []
        if codigo.tipo is TipoDeCodigo.INTERNAL_SKU:
            condiciones.append((Product.internal_sku == codigo.valor, "internal_sku"))
        elif codigo.tipo in (TipoDeCodigo.GTIN, TipoDeCodigo.SCALE):
            condiciones.append(
                (Product.barcode_normalized.in_(variantes_gtin(codigo.valor)), "barcode")
            )
        sku = normalize_sku(codigo.valor)
        if sku is not None:
            condiciones.append((Product.sku_normalized == sku, "sku"))

        encontrados: dict[UUID, tuple[Product, str]] = {}
        for condicion, columna in condiciones:
            filas = (
                await self._session.execute(
                    select(Product)
                    .where(
                        Product.tenant_id == tenant_id,
                        Product.is_active.is_(True),
                        condicion,
                    )
                    .order_by(Product.id)
                )
            ).scalars().all()
            for producto in filas:
                encontrados.setdefault(producto.id, (producto, columna))
        return list(encontrados.values())

    async def list_by_tenant(
        self,
        tenant_id: UUID,
        is_active: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Product]:
        q = select(Product).where(Product.tenant_id == tenant_id)
        if is_active is not None:
            q = q.where(Product.is_active == is_active)
        q = q.limit(limit).offset(offset)
        result = await self._session.execute(q)
        return list(result.scalars().all())

    async def count_by_tenant(
        self,
        tenant_id: UUID,
        is_active: bool | None = None,
    ) -> int:
        """Total de productos que ve `list_by_tenant` — mismos filtros, sin
        `limit`/`offset`. Corrección C4 (revisión externa 2026-08-19): el
        frontend acumula hasta 5000 productos (`getAllProducts`) sin forma de
        saber si ese límite se alcanzó de verdad."""
        q = select(func.count(Product.id)).where(Product.tenant_id == tenant_id)
        if is_active is not None:
            q = q.where(Product.is_active == is_active)
        result = await self._session.execute(q)
        return int(result.scalar_one() or 0)

    async def get_products_with_margin(self, tenant_id: UUID) -> list[dict[str, Any]]:
        """Catálogo activo con margen bruto calculado por producto (Sprint 17).

        Solo calcula margen donde `unit_cost_ars` y `sale_price_ars` están presentes
        y `sale_price_ars > 0`. Los productos sin costo se devuelven con
        `margin_pct=None` (NO se inventa un margen — política no-invention).

        Returns list[dict] con: product_id, name, sku, category, sale_price,
        unit_cost, stock_units, margin_pct (float|None), margin_abs (float|None).
        """
        result = await self._session.execute(
            select(Product).where(
                Product.tenant_id == tenant_id,
                Product.is_active.is_(True),
            )
        )
        products = list(result.scalars().all())
        out: list[dict[str, Any]] = []
        for p in products:
            sale_price = float(p.sale_price_ars or 0)
            unit_cost = float(p.unit_cost_ars) if p.unit_cost_ars is not None else None
            margin_pct: float | None = None
            margin_abs: float | None = None
            if unit_cost is not None and sale_price > 0:
                margin_abs = round(sale_price - unit_cost, 2)
                margin_pct = round((sale_price - unit_cost) / sale_price * 100, 1)
            out.append(
                {
                    "product_id": str(p.id),
                    "name": p.name,
                    "sku": p.sku,
                    "category": p.category,
                    "sale_price": sale_price,
                    "unit_cost": unit_cost,
                    "stock_units": p.stock_units,
                    # El costo es por unidad de venta; para valuar stock hay que dividir.
                    "base_units_per_sale_unit": p.base_units_per_sale_unit or 1,
                    "margin_pct": margin_pct,
                    "margin_abs": margin_abs,
                }
            )
        return out

    async def save(self, product: Product) -> Product:
        # F3-T3: shared lock ANTES de crear/actualizar el producto — barrera de
        # exclusión mutua real contra el dedup (que toma el exclusive). No-op en SQLite.
        await maintenance_lock_service.acquire_write_lock_shared(
            self._session, product.tenant_id
        )
        self._session.add(product)
        await self._session.flush()
        return product
