"""B·5b: registrar una compra de mercadería — un comprobante con artículos.

La regla de costo es una sola (``domain/comprobante_de_compra.py``) y la usan
todas las rutas. Este servicio la ESCRIBE para las rutas que cargan stock con
``stock_service.increment_stock``: la compra manual, la resolución desde
«Otros» y el chat. (El remito usa la misma regla con el pipeline del import.)

Todo va en la transacción del caller: líneas, un gasto de mercadería por línea
(con producto, proveedor, grupo y procedencia), un movimiento ``purchase`` por
línea con su contrato, el envío aparte si lo hay, el costo vigente y la
auditoría. Si algo falla, el caller revierte y no queda nada a medias. La
idempotencia la da cada ruta: ``Idempotency-Key`` en HTTP, el registro de
«Otros» bloqueado y PENDING, la acción pendiente del chat.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import maintenance_lock_service, stock_service
from app.domain.comprobante_de_compra import (
    ComprobanteInvalidoError,
    Envio,
    Iva,
    LineaDeCompra,
    costear_comprobante,
)
from app.domain.product_completion import recompute_requires_completion
from app.domain.sale_unit import a_unidades_base
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.product import Product
from app.persistence.models.supplier import Supplier
from app.persistence.models.transaction import ExpenseEntry


class CompraSinArticulosError(Exception):
    """El chat (u otra ruta) quiso registrar mercadería sin decir qué artículos.

    ``user_message`` lo muestra el chat tal cual (``api/v1/agent.py``).
    """

    user_message = (
        "Para registrar una compra de mercadería necesito saber qué artículos "
        "compraste y cuántos de cada uno. Decímelos (o cargala en «Registrar "
        "compra») y la registro."
    )


@dataclass(frozen=True)
class CompraRegistrada:
    group_id: uuid.UUID
    expense_ids: list[uuid.UUID]
    movement_ids: list[uuid.UUID]
    shipping_expense_id: uuid.UUID | None
    total: Decimal
    supplier_id: uuid.UUID
    supplier_name: str | None


async def _proveedor(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    supplier_id: uuid.UUID | None,
    supplier_name: str | None,
) -> tuple[uuid.UUID, str | None]:
    # Lazy: ingestion_import_service importa este módulo indirectamente.
    from app.application.services.ingestion_import_service import (  # noqa: PLC0415
        _load_supplier_index,
        _resolve_or_create_sentinel_supplier,
        _resolve_or_create_supplier,
    )

    if supplier_id is not None:
        supplier = (
            await session.execute(
                select(Supplier).where(
                    Supplier.id == supplier_id, Supplier.tenant_id == tenant_id
                )
            )
        ).scalar_one_or_none()
        if supplier is None:
            raise ComprobanteInvalidoError("Proveedor no encontrado para este negocio.")
        return supplier.id, supplier.name
    if supplier_name and supplier_name.strip():
        index = await _load_supplier_index(session, tenant_id)
        sid, nombre = await _resolve_or_create_supplier(
            session, tenant_id, supplier_name, index
        )
        if sid is not None:
            return sid, nombre
    # Sin proveedor: el centinela «No identificado», nunca un proveedor inventado.
    return await _resolve_or_create_sentinel_supplier(session, tenant_id, {}), None


async def registrar_compra(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
    *,
    lineas: list[LineaDeCompra],
    fecha: datetime,
    medio_de_pago: str,
    envio: Envio | None = None,
    iva: Iva | None = None,
    supplier_id: uuid.UUID | None = None,
    supplier_name: str | None = None,
    document_ref: str | None = None,
    descripciones: dict[uuid.UUID, str] | None = None,
    source_event_id: str | None = None,
    source_upload_id: uuid.UUID | None = None,
    source_row_ref: str | None = None,
    triggered_by: str = "ui:manual_purchase",
) -> CompraRegistrada:
    """Registra la compra. ``ComprobanteInvalidoError`` si no se puede."""
    costeado = costear_comprobante(lineas, envio=envio, iva=iva, document_ref=document_ref)

    # Advisory shared SIEMPRE antes que cualquier FOR UPDATE de fila: si no,
    # deadlockea AB-BA contra el exclusive del dedup (F3).
    await maintenance_lock_service.acquire_write_lock_shared(session, tenant_id)
    ids = sorted({linea.product_id for linea in lineas})
    productos = {
        p.id: p
        for p in (
            await session.execute(
                select(Product)
                .where(
                    Product.tenant_id == tenant_id,
                    Product.id.in_(ids),
                    Product.is_active.is_(True),
                )
                .order_by(Product.id)
                .with_for_update()
            )
        ).scalars()
    }
    faltan = [pid for pid in ids if pid not in productos]
    if faltan:
        raise ComprobanteInvalidoError(
            "Uno de los artículos no existe o está dado de baja. Elegilo de nuevo."
        )

    proveedor_id, proveedor_nombre = await _proveedor(
        session, tenant_id, supplier_id, supplier_name
    )
    group_id = uuid.uuid4()
    evento = source_event_id or f"purchase:{group_id}"
    expense_ids: list[uuid.UUID] = []
    movement_ids: list[uuid.UUID] = []
    ahora = datetime.now(UTC)
    for linea in costeado.lineas:
        producto = productos[linea.product_id]
        movimiento = await stock_service.increment_stock(
            producto.id,
            tenant_id,
            # La compra está en unidad de VENTA; el stock, en unidades base. El
            # costo es por unidad de venta.
            a_unidades_base(linea.cantidad, producto.base_units_per_sale_unit or 1),
            linea.costo_unitario,
            evento,
            session,
            supplier_id=proveedor_id,
            occurred_at=fecha,
            defer_event=True,
            contrato=linea.contrato,
            source_upload_id=source_upload_id,
            source_row_ref=source_row_ref,
        )
        movement_ids.append(movimiento.id)
        # Comprar un producto creado incompleto por un import le da costo y stock:
        # se recalcula si sigue «por completar» (antes lo hacía resolve-purchase).
        recompute_requires_completion(producto)
        gasto = ExpenseEntry(
            tenant_id=tenant_id,
            product_id=producto.id,
            supplier_id=proveedor_id,
            supplier_name=proveedor_nombre,
            amount=linea.importe_gasto,
            category="INVENTORY",
            expense_type="COGS",
            transaction_date=fecha,
            description=(descripciones or {}).get(producto.id) or f"Compra: {producto.name}",
            payment_method=medio_de_pago,
            custom_fields={"purchase_group_id": str(group_id)},
            provenance="REAL",
            source_upload_id=source_upload_id,
            source_row_ref=source_row_ref,
        )
        session.add(gasto)
        await session.flush()
        expense_ids.append(gasto.id)

    shipping_id: uuid.UUID | None = None
    if costeado.gasto_envio > 0:
        envio_gasto = ExpenseEntry(
            tenant_id=tenant_id,
            supplier_id=proveedor_id,
            supplier_name=proveedor_nombre,
            amount=costeado.gasto_envio,
            category="LOGISTICS",
            expense_type="OPEX",
            transaction_date=fecha,
            description="Envío de la compra",
            payment_method=medio_de_pago,
            custom_fields={"purchase_group_id": str(group_id)},
            provenance="REAL",
            source_upload_id=source_upload_id,
            source_row_ref=source_row_ref,
        )
        session.add(envio_gasto)
        await session.flush()
        shipping_id = envio_gasto.id

    session.add(
        DecisionAuditLog(
            tenant_id=tenant_id,
            decision_type="PURCHASE_REGISTERED",
            decision_data={
                "purchase_group_id": str(group_id),
                "lineas": [
                    {
                        "product_id": str(linea.product_id),
                        "cantidad": linea.cantidad,
                        "importe_gasto": str(linea.importe_gasto),
                        "costo_unitario": str(linea.costo_unitario),
                        "envio": linea.contrato.shipping_treatment,
                        "iva": linea.contrato.tax_treatment,
                    }
                    for linea in costeado.lineas
                ],
                "gasto_envio": str(costeado.gasto_envio),
                "expense_ids": [str(e) for e in expense_ids],
                "source_upload_id": str(source_upload_id) if source_upload_id else None,
            },
            triggered_by=triggered_by,
            actor_user_id=actor_user_id,
            created_at=ahora,
        )
    )
    return CompraRegistrada(
        group_id=group_id,
        expense_ids=expense_ids,
        movement_ids=movement_ids,
        shipping_expense_id=shipping_id,
        total=costeado.total,
        supplier_id=proveedor_id,
        supplier_name=proveedor_nombre,
    )
