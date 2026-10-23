"""Un producto por peso o volumen no se mueve por chat.

El chat extrae cantidades ENTERAS y sin unidad: "2 de queso" no dice si son
2 kg o 2 g, y el stock de un producto por peso vive en unidades base. Antes que
adivinar —y mover 1000 veces más o menos mercadería de la que se quiso—, el
agente pide hacerlo donde la cantidad se carga con su unidad.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.product import Product


async def nombre_si_es_por_peso(
    db: AsyncSession | None, tenant_id: str, product_id: str | None
) -> str | None:
    """El nombre del producto si se vende por peso o volumen (factor > 1)."""
    if db is None or not product_id:
        return None
    try:
        pid = uuid.UUID(str(product_id))
        tid = uuid.UUID(str(tenant_id))
    except ValueError:
        return None
    fila = (
        await db.execute(
            select(Product.name, Product.base_units_per_sale_unit).where(
                Product.id == pid, Product.tenant_id == tid
            )
        )
    ).first()
    if fila is None or (fila.base_units_per_sale_unit or 1) <= 1:
        return None
    return str(fila.name)


def pregunta_por_peso(nombre: str, donde: str) -> str:
    return (
        f"'{nombre}' se vende por peso o volumen y el chat no sabe en qué unidad "
        f"está la cantidad. Cargalo desde {donde}, donde se escribe en kg o litros."
    )
