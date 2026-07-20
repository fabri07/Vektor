"""Compra manual de mercadería: comprobante multi-línea, transaccional.

Por cada línea: resuelve/crea el producto, suma stock (movimiento ``purchase``) y
crea el gasto COGS (categoría INVENTORY) asociado al proveedor. Todo o nada: si
una línea falla, la transacción entera revierte (la sesión hace rollback ante la
excepción) — nunca queda producto sin gasto ni gasto sin stock.

Reusa ``stock_service.increment_stock`` (stock + movimiento + costo histórico) y
``normalize_product_category`` (catálogo del vertical).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import (
    maintenance_lock_service,
    stock_service,
    tenant_categories_service,
)
from app.application.services.product_identity import (
    ProductIdentityConflictError,
    add_product_or_reuse,
)
from app.domain.product_categories import normalize_product_category
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.business import BusinessProfile
from app.persistence.models.product import Product
from app.persistence.models.supplier import Supplier
from app.persistence.models.transaction import ExpenseEntry
from app.schemas.purchase import (
    ManualPurchaseRequest,
    ManualPurchaseResponse,
    PurchaseLine,
    PurchaseLineResult,
)


class PurchaseError(ValueError):
    """Error de negocio en la compra manual (se mapea a HTTP 400 en el router)."""


def _margin_pct(unit_cost: Decimal, sale_price: Decimal) -> float | None:
    if sale_price <= 0:
        return None
    return round(float((sale_price - unit_cost) / sale_price * 100), 1)


async def _business_type(session: AsyncSession, tenant_id: uuid.UUID) -> str | None:
    result = await session.execute(
        select(BusinessProfile.vertical_code).where(BusinessProfile.tenant_id == tenant_id)
    )
    return result.scalar_one_or_none()


def _audit(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    decision_type: str,
    record_type: str,
    record_id: str,
    group_id: uuid.UUID,
) -> None:
    session.add(
        DecisionAuditLog(
            tenant_id=tenant_id,
            decision_type=decision_type,
            decision_data={
                "record_type": record_type,
                "record_id": record_id,
                "purchase_group_id": str(group_id),
                "source": "ui",
            },
            triggered_by="ui:manual_purchase",
            actor_user_id=user_id,
            context={"endpoint": "purchases"},
            created_at=datetime.now(UTC),
        )
    )


async def register_manual_purchase(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    body: ManualPurchaseRequest,
    actor_user_id: uuid.UUID,
) -> ManualPurchaseResponse:
    # Proveedor obligatorio y del tenant.
    supplier = (
        await session.execute(
            select(Supplier).where(
                Supplier.id == body.supplier_id, Supplier.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()
    if supplier is None:
        raise PurchaseError("Proveedor no encontrado para este negocio.")

    # F3 review final: el advisory shared SIEMPRE antes que cualquier FOR UPDATE de fila
    # (acá abajo) — si no, deadlockea AB-BA contra el exclusive del script de dedup. Antes
    # el shared recién se tomaba en increment_stock, después del FOR UPDATE de este método.
    # Idempotente dentro de la txn: no molesta que stock_service lo pida de nuevo.
    await maintenance_lock_service.acquire_write_lock_shared(session, tenant_id)

    # Rechazar productos existentes repetidos en el comprobante.
    existing_ids = [line.product_id for line in body.lines if line.product_id is not None]
    if len(set(existing_ids)) != len(existing_ids):
        raise PurchaseError(
            "Hay productos repetidos en el comprobante. Unificá la cantidad en una sola línea."
        )
    # Bloquear las filas de productos existentes (orden estable) — anti-deadlock.
    if existing_ids:
        await session.execute(
            select(Product)
            .where(
                Product.tenant_id == tenant_id,
                Product.id.in_(existing_ids),
                Product.is_active.is_(True),
            )
            .order_by(Product.id)
            .with_for_update()
        )

    business_type = await _business_type(session, tenant_id)
    group_id = uuid.uuid4()
    source_event = f"manual_purchase:{group_id}"

    results: list[PurchaseLineResult] = []
    products_created: list[uuid.UUID] = []
    expense_ids: list[uuid.UUID] = []
    total_cogs = Decimal("0")

    for line in body.lines:
        product, created = await _resolve_product(session, tenant_id, business_type, line)

        await stock_service.increment_stock(
            product.id,
            tenant_id,
            line.quantity,
            line.unit_cost,
            source_event,
            session,
            supplier_id=body.supplier_id,
            # Producto nuevo o con confirmación → actualizar costo de catálogo.
            update_product_cost=created or line.update_price,
            occurred_at=body.transaction_date,
        )
        # Precio de venta: nuevo siempre; existente solo si lo confirma.
        if (created or line.update_price) and line.sale_price_ars > 0:
            product.sale_price_ars = line.sale_price_ars

        line_cogs = (line.unit_cost * line.quantity).quantize(Decimal("0.01"))
        expense = ExpenseEntry(
            tenant_id=tenant_id,
            product_id=product.id,
            supplier_id=body.supplier_id,
            amount=line_cogs,
            category="INVENTORY",
            expense_type="COGS",
            transaction_date=body.transaction_date,
            description=line.description or f"Compra: {product.name}",
            payment_method=body.payment_method,
            supplier_name=supplier.name,
            custom_fields={"purchase_group_id": str(group_id)},
        )
        session.add(expense)
        await session.flush()

        # Auditar: alta/actualización de producto + alta de gasto COGS (mismo group_id).
        _audit(
            session,
            tenant_id,
            actor_user_id,
            "DATA_RECORD_CREATED" if created else "DATA_RECORD_UPDATED",
            "product",
            str(product.id),
            group_id,
        )
        _audit(
            session,
            tenant_id,
            actor_user_id,
            "DATA_RECORD_CREATED",
            "expense",
            str(expense.id),
            group_id,
        )

        total_cogs += line_cogs
        expense_ids.append(expense.id)
        if created:
            products_created.append(product.id)
        results.append(
            PurchaseLineResult(
                product_id=product.id,
                product_name=product.name,
                created=created,
                expense_id=expense.id,
                new_stock_units=product.stock_units,
                margin_pct=_margin_pct(line.unit_cost, product.sale_price_ars),
            )
        )

    return ManualPurchaseResponse(
        lines=len(body.lines),
        products_created=products_created,
        expense_ids=expense_ids,
        total_cogs=float(total_cogs),
        results=results,
        meta={"purchase_group_id": str(group_id), "supplier_name": supplier.name},
    )


async def _resolve_product(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    business_type: str | None,
    line: PurchaseLine,
) -> tuple[Product, bool]:
    """Devuelve ``(producto, creado)``. Existente por id, o crea uno nuevo."""
    if line.product_id is not None:
        product = (
            await session.execute(
                select(Product).where(
                    Product.id == line.product_id,
                    Product.tenant_id == tenant_id,
                    Product.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if product is None:
            raise PurchaseError(f"Producto {line.product_id} no encontrado.")
        return product, False

    # Producto nuevo: nombre y categoría requeridos.
    name = (line.name or "").strip()
    if not name:
        raise PurchaseError("Falta el nombre del producto nuevo.")
    if not (line.category or "").strip():
        raise PurchaseError(f"Falta la categoría del producto «{name}».")

    # Categoría custom del tenant primero; si no, catálogo del vertical.
    custom_cat = await tenant_categories_service.resolve_custom_product_category(
        session, tenant_id, line.category or ""
    )
    if custom_cat is not None:
        code, label = custom_cat["code"], None
    else:
        code, label = normalize_product_category(line.category, business_type)
    custom_fields: dict[str, str] = {}
    if label:
        custom_fields["category_label"] = label

    product = Product(
        tenant_id=tenant_id,
        name=name,
        sku=(line.sku or None),
        description=(line.description or None),
        sale_price_ars=line.sale_price_ars,
        unit_cost_ars=line.unit_cost,
        category=code,
        stock_units=0,  # el ingreso lo aplica increment_stock
        custom_fields=custom_fields,
    )
    # F5-A: alta INTERACTIVA (el usuario está cargando una compra a mano), así que
    # NO se reusa en silencio: si el SKU ya está tomado por otro producto activo,
    # elegirle uno sería adivinar sobre una decisión suya. Se le dice cuál es y
    # decide él —vincular la línea a ese producto, o corregir el SKU—.
    try:
        _resolved, _created = await add_product_or_reuse(
            session, product, on_conflict="raise"
        )
    except ProductIdentityConflictError as conflict:
        clave = "código de barras" if conflict.matched_by == "barcode" else "SKU"
        raise PurchaseError(
            f"El {clave} del producto «{name}» ya pertenece a "
            f"«{conflict.existing.name}». Vinculá la línea a ese producto o "
            f"corregí el {clave}."
        ) from conflict
    await session.flush()  # asigna product.id
    return product, True
