"""F-H1: el orden de las hojas en el Excel no decide si una venta se vincula.

Un libro con la hoja de Ventas ANTES que la de Productos es la forma más
común de exportar un negocio: primero el movimiento, después el catálogo. Hoy
el importador recorre los contextos en el orden del archivo, así que esas
ventas se resuelven contra un catálogo que todavía no existe y quedan sin
producto — y el MISMO archivo con las hojas al revés sí las vincula.

Que un dato de negocio dependa de cómo el usuario ordenó las solapas es la
misma clase de bug que el orden de las columnas: se decide por un detalle de
implementación.

Son DOS causas independientes y hay que cerrar las dos:
  1. el recorrido va en el orden del archivo;
  2. `_add_product` no registra lo que crea en los índices que consulta
     `_resolve_product` — la ruta de compras sí lo hace, la de catálogo no.
Arreglar sólo el orden deja el test rojo, y por eso el segundo caso mira una
hoja de catálogo aunque venga primera.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.application.services.ingestion_import_service as importer
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry

_VENTAS = "sheet:ventas"
_PRODUCTOS = "sheet:productos"


def _summary(*, ventas_primero: bool) -> dict[str, Any]:
    """El mismo libro, con las dos solapas en un orden o en el otro."""
    ventas_ctx = {
        "context_id": _VENTAS,
        "label": "Ventas",
        "source_kind": "sheet",
        "entity_type": "sale",
        "headers": ["fecha", "producto", "monto"],
        "fields": None,
        "preview_rows": [],
        "row_count": 1,
    }
    productos_ctx = {
        "context_id": _PRODUCTOS,
        "label": "Productos",
        "source_kind": "sheet",
        "entity_type": "product",
        "headers": ["nombre", "precio"],
        "fields": None,
        "preview_rows": [],
        "row_count": 1,
    }
    contexts = (
        [ventas_ctx, productos_ctx] if ventas_primero else [productos_ctx, ventas_ctx]
    )
    return {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "confidence": "HIGH",
        "has_venta": True,
        "has_producto": True,
        "row_count": 2,
        "ventas_detectadas": [
            {
                "fecha": "2024-03-10",
                "producto": "Vela aromática 200g",
                "monto": "2100",
                "__context__": _VENTAS,
            }
        ],
        "stock_detectado": [
            {
                "nombre": "Vela aromática 200g",
                "precio": "2100",
                "__context__": _PRODUCTOS,
            }
        ],
        "mapping_contexts": contexts,
    }


_MAPPINGS: dict[str, dict[str, str]] = {
    _VENTAS: {
        "fecha": "transaction_date",
        "producto": "product_name",
        "monto": "amount",
    },
    _PRODUCTOS: {"nombre": "name", "precio": "sale_price_ars"},
}
_CONFIRMED = {_VENTAS: True, _PRODUCTOS: True}


async def _importar(
    db: AsyncSession, tenant: Tenant, *, ventas_primero: bool
) -> None:
    await importer.insert_confirmed_data(
        db,
        tenant.tenant_id,
        _summary(ventas_primero=ventas_primero),
        {"ventas": True, "productos": True},
        context_mappings=_MAPPINGS,
        context_confirmed=_CONFIRMED,
    )


@pytest.mark.asyncio
class TestOrdenDeHojas:
    async def test_venta_vincula_aunque_su_hoja_venga_primera(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """El caso que hoy falla: Ventas en la solapa 1, Productos en la 2."""
        await _importar(db_session, sample_tenant, ventas_primero=True)

        producto = (await db_session.execute(select(Product))).scalars().one()
        venta = (await db_session.execute(select(SaleEntry))).scalars().one()
        assert venta.product_id == producto.id

    async def test_el_orden_inverso_da_el_mismo_resultado(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Control: el mismo libro con las solapas al revés.

        Si este pasara y el anterior no, la vinculación estaría decidida por el
        orden de las solapas — que es exactamente lo que se está cerrando.
        """
        await _importar(db_session, sample_tenant, ventas_primero=False)

        producto = (await db_session.execute(select(Product))).scalars().one()
        venta = (await db_session.execute(select(SaleEntry))).scalars().one()
        assert venta.product_id == producto.id
