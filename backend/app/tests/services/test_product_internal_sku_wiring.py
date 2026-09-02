"""El código interno se asigna en TODA alta de producto y nunca se regenera.

La generación pura vive en `app/tests/domain/test_internal_sku.py`. Acá se prueba
el WIRING: el listener `before_insert` de `Product`, que es el único hook que
cubre las cinco rutas de alta (import de catálogo, import de compra, chat,
remito, POST manual) sin depender de que cada constructor se acuerde.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.ingestion_import_service import insert_confirmed_data
from app.domain.internal_sku import generate_internal_sku, is_internal_sku
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant

pytestmark = pytest.mark.asyncio

_CTX = "sheet:Catalogo"


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
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
                "headers": ["nombre", "precio_venta"],
                "row_count": len(rows),
            },
        ],
        "stock_detectado": [{**r, "__context__": _CTX} for r in rows],
    }


async def _importar(session: AsyncSession, tid: Any, rows: list[dict[str, Any]]) -> None:
    await insert_confirmed_data(
        session, tid, _summary(rows), {"productos": True}, context_confirmed={_CTX: True}
    )


async def _productos(session: AsyncSession, tid: Any) -> list[Product]:
    return list(
        (await session.execute(select(Product).where(Product.tenant_id == tid)))
        .scalars()
        .all()
    )


async def test_un_producto_creado_a_mano_recibe_su_codigo(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    producto = Product(
        tenant_id=sample_tenant.tenant_id,
        name="Alfombra nórdica",
        sale_price_ars=Decimal("15000"),
        stock_units=3,
    )
    db_session.add(producto)
    await db_session.flush()

    assert is_internal_sku(producto.internal_sku)
    # Determinístico desde el id: se puede recomputar sin leer la fila.
    assert producto.internal_sku == generate_internal_sku(producto.id)


async def test_el_import_le_pone_codigo_a_productos_sin_sku(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """El caso que lo motivó: un catálogo de 398 productos, ninguno con SKU."""
    tid = sample_tenant.tenant_id
    await _importar(
        db_session,
        tid,
        [
            {"nombre": "Bandeja ginko", "precio_venta": "7000"},
            {"nombre": "Vela de soja", "precio_venta": "4000"},
            {"nombre": "Manta polar", "precio_venta": "9000"},
        ],
    )

    productos = await _productos(db_session, tid)
    assert len(productos) == 3
    assert all(p.sku is None for p in productos), "el archivo no traía SKU"
    codigos = {p.internal_sku for p in productos}
    assert all(is_internal_sku(c) for c in codigos)
    assert len(codigos) == 3, "cada producto tiene el suyo"


async def test_una_segunda_lectura_no_cambia_el_codigo(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """La propiedad que lo vuelve utilizable. Un código que cambia entre
    relecturas no sirve para etiquetar ni para buscar nada."""
    tid = sample_tenant.tenant_id
    fila = [{"nombre": "Bandeja ginko", "precio_venta": "7000"}]

    await _importar(db_session, tid, fila)
    antes = (await _productos(db_session, tid))[0].internal_sku

    await _importar(db_session, tid, fila)
    productos = await _productos(db_session, tid)

    assert len(productos) == 1, "la segunda lectura reusa el producto, no lo duplica"
    assert productos[0].internal_sku == antes


async def test_un_update_no_regenera_el_codigo(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """`before_update` no lo toca. Cambiar el nombre, la categoría o el proveedor
    de un producto no cambia su identidad comercial."""
    producto = Product(
        tenant_id=sample_tenant.tenant_id,
        name="Bandeja",
        sale_price_ars=Decimal("7000"),
        stock_units=1,
    )
    db_session.add(producto)
    await db_session.flush()
    original = producto.internal_sku

    producto.name = "Bandeja ginko calada"
    producto.sku = "PROV-9931"  # llega el código del proveedor: no desplaza al propio
    await db_session.flush()

    assert producto.internal_sku == original


async def test_un_codigo_ya_asignado_se_respeta(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """El guard es `is None`, no falsy: lo que ya tiene código no se pisa. Es lo
    que va a permitir que el backfill asigne códigos sin que el listener los
    reescriba después."""
    producto = Product(
        tenant_id=sample_tenant.tenant_id,
        name="Vela",
        sale_price_ars=Decimal("4000"),
        stock_units=1,
        internal_sku="VKT-ZZZZZZZZZZZZ",
    )
    db_session.add(producto)
    await db_session.flush()

    assert producto.internal_sku == "VKT-ZZZZZZZZZZZZ"


async def test_una_colision_de_codigo_propio_no_fusiona_productos(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """El invariante que más cuida este cambio.

    `add_product_or_reuse` traduce una violación de unicidad en "es el mismo
    producto, reusalo" — correcto para `sku` y `barcode`, que son claves del
    NEGOCIO. Para un código que generamos nosotros sería exactamente lo
    contrario: dos productos distintos que sacaron el mismo número no son el
    mismo producto.

    Lo que lo evita es que `uq_products_tenant_internal_sku` NO está en
    `_UQ_NAMES`, así que `_violated_identity_index` devuelve `None` y el error se
    propaga. Este test lo fija: es un invariante que se rompe agregando una línea
    de aspecto inofensivo.
    """
    from app.application.services.product_identity import _UQ_NAMES

    assert "uq_products_tenant_internal_sku" not in _UQ_NAMES, (
        "agregar el índice del código propio a _UQ_NAMES haría que una colisión "
        "FUSIONE dos productos distintos en vez de fallar"
    )
