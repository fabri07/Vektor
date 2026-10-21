"""Lock compartido de ALTA de productos, tomado al insertar uno (B·5c).

El lote de stock inicial (``POST /products/batch``) calcula coincidencias por
nombre —que admite homónimos, así que no hay índice único que lo proteja— y
recién después inserta. Para que ningún producto nazca en el medio, el lote toma
el lock EXCLUSIVO del tenant y todo otro camino que cree productos toma el
COMPARTIDO: alta, «Otros», compra a mano, chat, import, relectura, reparaciones.

Son muchos caminos; por eso no se llama a mano en cada uno: este listener lo
toma en el ``before_flush`` de cualquier sesión que tenga un ``Product`` nuevo,
en cada flush con un producto nuevo. Compartido entre sí, dos altas o un import y un alta
no se esperan; sólo esperan (poco) mientras un lote está confirmando.

Sólo Postgres. En SQLite no hay advisory locks ni concurrencia real.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app.persistence.models.product import Product

#: Espacio propio del lock: la forma de dos claves ``(int4, int4)``, que en
#: Postgres no se superpone con la de una clave ``bigint`` del lock de
#: mantenimiento (``maintenance_lock_service``): nunca chocan.
CLASE_ALTA_DE_PRODUCTO = 0x42354301


def clave_alta_de_producto(tenant_id: uuid.UUID) -> int:
    """La clave del tenant en ese espacio; la misma para el compartido de acá y
    el exclusivo del lote (``try_product_creation_exclusive``)."""
    return int.from_bytes(tenant_id.bytes[8:12], "big", signed=True)


@event.listens_for(Session, "before_flush")
def _tomar_lock_de_alta(session: Session, _ctx: Any, _instances: Any) -> None:
    nuevos = {
        obj.tenant_id
        for obj in session.new
        if isinstance(obj, Product) and obj.tenant_id is not None
    }
    if not nuevos:
        return
    conn = session.connection()
    if conn.dialect.name != "postgresql":
        return
    # En CADA flush con un producto nuevo, sin recordar los ya tomados: un
    # ROLLBACK TO SAVEPOINT suelta el lock pedido después de ese savepoint (el
    # import inserta sus lotes dentro de uno), y un caché lo daría por tomado.
    # Es reentrante: pedirlo de nuevo cuesta un round-trip, no espera.
    # Orden fijo: dos sesiones con dos tenants nuevos no se cruzan.
    for tenant_id in sorted(nuevos, key=str):
        conn.execute(
            text("SELECT pg_advisory_xact_lock_shared(:c, :k)"),
            {"c": CLASE_ALTA_DE_PRODUCTO, "k": clave_alta_de_producto(tenant_id)},
        )
