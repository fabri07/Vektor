"""La descripción del archivo se PERSISTE en el producto.

Hasta acá el importador nunca escribía `Product.description`: no era un problema
de mapeo —`CANONICAL_FIELDS["product"]["description"]` ya existía y el mapeador
ya ofrecía el campo— sino que el `Product(...)` simplemente no lo seteaba.
Medido en la cuenta real que lo destapó: 0 descripciones de 398 productos, con
una columna "Especificaciones" mapeada en el archivo.

Política, la misma que ya rige `barcode` y `category` en el mismo merge:
completar sólo si está vacío. Nunca pisa lo que hay, nunca lo vacía.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.ingestion_import_service import insert_confirmed_data
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant

pytestmark = pytest.mark.asyncio

_CTX = "sheet:Catalogo"
#: El mapeo que el usuario confirma en el panel. `description` sólo se persiste
#: por mapeo explícito — ver la nota de `_ESPECIFICACIONES_COLS` en el importador.
_MAPEO = {
    _CTX: {
        "nombre": "name",
        "especificaciones": "description",
        "precio_venta": "sale_price_ars",
    }
}


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    headers = ["nombre", "especificaciones", "precio_venta"]
    return {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "has_stock": True,
        "mapping_contexts": [
            {
                "context_id": _CTX,
                "label": "Catalogo",
                "entity_type": "product",
                "headers": headers,
                "row_count": len(rows),
            },
        ],
        "stock_detectado": [{**r, "__context__": _CTX} for r in rows],
    }


async def _importar(
    session: AsyncSession, tenant_id: Any, rows: list[dict[str, Any]]
) -> None:
    await insert_confirmed_data(
        session,
        tenant_id,
        _summary(rows),
        {"productos": True},
        context_mappings=_MAPEO,
        context_confirmed={_CTX: True},
    )


async def _producto(session: AsyncSession, tenant_id: Any, name: str) -> Product:
    return (
        await session.execute(
            select(Product).where(Product.tenant_id == tenant_id, Product.name == name)
        )
    ).scalar_one()


async def test_el_alta_persiste_la_descripcion_del_archivo(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    await _importar(
        db_session,
        sample_tenant.tenant_id,
        [{"nombre": "Alfombra nórdica", "especificaciones": "40 × 60, pelo suave",
          "precio_venta": "15000"}],
    )

    producto = await _producto(db_session, sample_tenant.tenant_id, "Alfombra nórdica")
    assert producto.description == "40 × 60, pelo suave"


async def test_una_descripcion_existente_no_se_pisa(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Segundo archivo con otra descripción sobre el mismo producto: gana la que
    ya estaba. Mismo criterio que `barcode` y `category` — no hay forma de saber
    cuál de los dos archivos tiene razón, así que no se elige por el usuario."""
    tid = sample_tenant.tenant_id
    await _importar(
        db_session, tid,
        [{"nombre": "Manta polar", "especificaciones": "2 plazas", "precio_venta": "9000"}],
    )
    await _importar(
        db_session, tid,
        [{"nombre": "Manta polar", "especificaciones": "OTRA COSA", "precio_venta": "9000"}],
    )

    producto = await _producto(db_session, tid, "Manta polar")
    assert producto.description == "2 plazas"


async def test_una_celda_vacia_no_borra_la_descripcion(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """El caso que más importa en una relectura: el archivo vuelve a leerse y la
    celda viene vacía. Vaciar la descripción sería perder un dato que el archivo
    no está contradiciendo, sólo omitiendo."""
    tid = sample_tenant.tenant_id
    await _importar(
        db_session, tid,
        [{"nombre": "Vela de soja", "especificaciones": "lavanda 200g",
          "precio_venta": "4000"}],
    )
    await _importar(
        db_session, tid,
        [{"nombre": "Vela de soja", "especificaciones": "", "precio_venta": "4000"}],
    )

    producto = await _producto(db_session, tid, "Vela de soja")
    assert producto.description == "lavanda 200g"


async def test_el_producto_sin_columna_de_descripcion_queda_en_none(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Sin mapeo explícito no se inventa una descripción desde una heurística:
    "detalle" es keyword de especificaciones Y de nombre, así que adivinar
    guardaría el nombre del producto como su descripción."""
    tid = sample_tenant.tenant_id
    await insert_confirmed_data(
        db_session,
        tid,
        _summary([{"nombre": "Bandeja ginko", "detalle": "madera", "precio_venta": "7000"}]),
        {"productos": True},
        context_confirmed={_CTX: True},
    )

    producto = await _producto(db_session, tid, "Bandeja ginko")
    assert producto.description is None
