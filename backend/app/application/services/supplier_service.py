"""Servicio de proveedores — consultas y persistencia de borradores.

Nota: la tabla `suppliers` existe en la DB (schema v1.1).
No hay ORM model para Supplier en esta fase; se usan queries raw.
save_supplier_draft guarda en tabla `supplier_drafts` — NO envía el correo.
"""

from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def get_approved_senders(business_id: str, db: AsyncSession) -> list[str]:
    """Retorna los emails de proveedores registrados para el negocio."""
    result = await db.execute(
        text("SELECT email FROM suppliers WHERE business_id = :business_id AND status = 'active'"),
        {"business_id": business_id},
    )
    rows = result.fetchall()
    return [row[0] for row in rows if row[0]]


async def get_supplier_by_email(
    email: str,
    business_id: str,
    db: AsyncSession,
) -> Optional[dict]:
    """Busca un proveedor por email dentro del negocio. Retorna None si no existe."""
    result = await db.execute(
        text(
            "SELECT supplier_id, business_id, name, email, phone, tags, status "
            "FROM suppliers WHERE email = :email AND business_id = :business_id"
        ),
        {"email": email.lower(), "business_id": business_id},
    )
    row = result.fetchone()
    if row is None:
        return None
    return {
        "supplier_id": str(row[0]),
        "business_id": str(row[1]),
        "name": row[2],
        "email": row[3],
        "phone": row[4],
        "tags": row[5],
        "status": row[6],
    }


async def save_supplier_draft(
    draft_content: str,
    supplier_id: str,
    business_id: str,
    db: AsyncSession,
) -> dict:
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
