"""Bloque 2 — vínculos Producto↔Proveedor declarados en catálogo (Tienda → proveedor).

Muchos-a-muchos, no 1:1: un producto puede repuesto haber salido de más de una
tienda/proveedor (caso real Asteria: "ganchos para cortina de baño" desde 'El
pasillo' Y 'sublink'). `source` distingue una declaración de catálogo (el
usuario mapeó la columna, sin evidencia de compra) de un vínculo respaldado por
evidencia real — y una vez en `purchase_evidence` nunca se degrada de vuelta:
una relectura que deja de declarar el vínculo por catálogo NO debe borrar
evidencia de compra real.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.product_supplier_link import ProductSupplierLink

#: Índice en memoria de vínculos por `(product_id, supplier_id)`. Ver
#: `load_declared_link_index`.
LinkIndex = dict[tuple[uuid.UUID, uuid.UUID], ProductSupplierLink]


async def load_declared_link_index(
    session: AsyncSession, tenant_id: uuid.UUID
) -> LinkIndex:
    """Todos los vínculos del tenant en UNA query, indexados por (producto, proveedor).

    Existe porque el find-or-create de abajo, llamado por fila de catálogo, hacía un
    SELECT + un `flush` cada vez: sobre el archivo real de Asteria con la flag de
    `product_supplier_links` encendida eran 251 SELECT + 238 INSERT sueltos, y ese
    flush por fila además rompía el agrupado de los movimientos de inventario
    (`scripts/bench_confirm_import.py`: 250 statements sin la flag, 1.010 con ella).
    """
    filas = (
        (
            await session.execute(
                select(ProductSupplierLink).where(
                    ProductSupplierLink.tenant_id == tenant_id
                )
            )
        )
        .scalars()
        .all()
    )
    return {(f.product_id, f.supplier_id): f for f in filas}


async def link_product_to_declared_supplier(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    product_id: uuid.UUID,
    supplier_id: uuid.UUID,
    *,
    source: str,
    source_upload_id: uuid.UUID | None,
    source_context_id: str | None,
    link_index: LinkIndex | None = None,
) -> ProductSupplierLink:
    """Find-or-create/revive el vínculo (tenant, product, supplier).

    Idempotente: dos filas repetidas del mismo archivo (o dos corridas de la
    misma relectura) resuelven al MISMO row, nunca lo duplican — la unicidad
    real la garantiza el índice `uq_product_supplier_links_tenant_product_supplier`
    de la migración; esto solo evita pagar un `IntegrityError` en el camino feliz.

    El `source` SOLO puede subir de `catalog_declared` a `purchase_evidence`,
    nunca al revés — degradar borraría evidencia real de compra por una
    relectura que simplemente no volvió a mapear la columna.

    ``link_index``: índice precargado (ver `load_declared_link_index`). Con él, el
    find-or-create no toca la base y el INSERT queda pendiente para el flush por
    lote del import. Sin él se conserva el camino de a uno —SELECT + `flush`— que
    usan los callers que resuelven un vínculo suelto.

    El ``flush`` del camino sin índice NO es decorativo: con ``autoflush=False``
    (producción) el SELECT de la fila siguiente no vería el vínculo recién
    agregado, y dos filas del mismo archivo con el mismo par intentarían insertarlo
    dos veces. Con índice, esa función la cumple el propio índice, que se actualiza
    en memoria.
    """
    if link_index is not None:
        existing = link_index.get((product_id, supplier_id))
    else:
        existing = (
            await session.execute(
                select(ProductSupplierLink).where(
                    ProductSupplierLink.tenant_id == tenant_id,
                    ProductSupplierLink.product_id == product_id,
                    ProductSupplierLink.supplier_id == supplier_id,
                )
            )
        ).scalar_one_or_none()

    if existing is None:
        link = ProductSupplierLink(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            product_id=product_id,
            supplier_id=supplier_id,
            source=source,
            source_upload_id=source_upload_id,
            source_context_id=source_context_id,
        )
        session.add(link)
        if link_index is None:
            await session.flush()
        else:
            link_index[(product_id, supplier_id)] = link
        return link

    existing.voided_at = None
    if existing.source == "catalog_declared" and source == "purchase_evidence":
        existing.source = "purchase_evidence"
    if source_upload_id is not None:
        existing.source_upload_id = source_upload_id
    if source_context_id is not None:
        existing.source_context_id = source_context_id
    return existing


async def reconcile_catalog_declared_links_for_upload(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    source_upload_id: uuid.UUID,
    still_declared_pairs: set[tuple[uuid.UUID, uuid.UUID]],
) -> int:
    """Anula (soft-delete) los vínculos `catalog_declared` de ESTE archivo que
    una relectura dejó de declarar.

    Solo toca `source='catalog_declared'` con este `source_upload_id` — nunca
    `purchase_evidence` (aunque haya llegado del mismo archivo) ni vínculos de
    otro archivo. Devuelve cuántos se anularon.
    """
    rows = (
        await session.execute(
            select(ProductSupplierLink).where(
                ProductSupplierLink.tenant_id == tenant_id,
                ProductSupplierLink.source_upload_id == source_upload_id,
                ProductSupplierLink.source == "catalog_declared",
                ProductSupplierLink.voided_at.is_(None),
            )
        )
    ).scalars().all()

    voided = 0
    for row in rows:
        if (row.product_id, row.supplier_id) in still_declared_pairs:
            continue
        row.voided_at = datetime.now(UTC)
        voided += 1
    return voided
