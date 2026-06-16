"""Servicio de proveedores — consultas sobre la entidad Supplier (Fase 3).

Las lecturas usan el ORM `Supplier` (tabla `suppliers`, tenant-scoped). El
parámetro `tenant_id` es el identificador multi-tenant; los call sites legacy lo
pasaban como `business_id`. "Activo" = `deactivated_at IS NULL`.

`save_supplier_draft` (tabla `supplier_drafts`) sigue pendiente de Fase 5
(comunicación real): esa tabla aún no existe.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.supplier import Supplier


async def get_approved_senders(tenant_id: str, db: AsyncSession) -> list[str]:
    """Emails de proveedores activos del tenant (para el preflight de Gmail)."""
    result = await db.execute(
        select(Supplier.email).where(
            Supplier.tenant_id == tenant_id,
            Supplier.deactivated_at.is_(None),
            Supplier.email.isnot(None),
        )
    )
    return [row[0] for row in result.all() if row[0]]


async def get_supplier_by_email(
    email: str,
    tenant_id: str,
    db: AsyncSession,
) -> dict[str, Any] | None:
    """Busca un proveedor activo por email dentro del tenant. None si no existe."""
    result = await db.execute(
        select(Supplier).where(
            Supplier.tenant_id == tenant_id,
            Supplier.email == email.lower(),
        )
    )
    supplier = result.scalars().first()
    if supplier is None:
        return None
    return {
        "supplier_id": str(supplier.id),
        "business_id": str(supplier.tenant_id),
        "name": supplier.name,
        "email": supplier.email,
        "phone": supplier.phone,
        "tags": None,  # legacy field; no aplica a la entidad Supplier nueva
        "status": "active" if supplier.deactivated_at is None else "inactive",
    }


async def save_supplier_draft(
    draft_content: str,
    supplier_id: str,
    business_id: str,
    db: AsyncSession,
) -> dict[str, Any]:
    """Guarda un borrador de respuesta a proveedor. NUNCA envía el correo."""
    draft_id = str(uuid.uuid4())
    await db.execute(
        text(
            "INSERT INTO supplier_drafts (draft_id, supplier_id, business_id, content, status) "
            "VALUES (:draft_id, :supplier_id, :business_id, :content, 'PENDING')"
        ),
        {
            "draft_id": draft_id,
            "supplier_id": supplier_id,
            "business_id": business_id,
            "content": draft_content,
        },
    )
    return {
        "draft_id": draft_id,
        "supplier_id": supplier_id,
        "business_id": business_id,
        "content": draft_content,
        "status": "PENDING",
    }
