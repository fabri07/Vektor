"""F-H3.d — la venta importada tiene que saber de qué hoja vino.

Sin esto el replay sólo podría aplicarse al archivo entero. Un libro con una hoja
de ventas viejas marcada `informational` y otra de ventas del mes marcada
`historical_replay` terminaría descontando las dos, que es exactamente lo que el
eje por hoja vino a evitar. `source_row_ref` no sirve para reconstruirlo: es el
sha256 del ancla y no se puede volver atrás (V18).

Los DOS caminos de inserción tienen que estamparlo. El de una sola tabla no
recorre contextos —no tiene por qué, hay uno solo—, y por eso venía perdiendo el
contexto entero: ni estampaba la hoja en la venta ni le pasaba el efecto
declarado a la proyección, así que un `.xlsx` plano quedaba clavado en el default
aunque el usuario hubiera elegido otra cosa.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.application.services.ingestion_import_service as importer
from app.domain.inventory_effect import IMPORT_CONTEXT_FIELD
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry

_VENTAS = "sheet:ventas"
_TABLA = "table:0"
_PRODUCTO = "Vela aromática 200g"

_MAPPING_VENTAS = {
    "fecha": "transaction_date",
    "producto": "product_name",
    "cantidad": "quantity",
    "monto": "amount",
}


def _fila_venta(context_id: str | None = None) -> dict[str, Any]:
    fila = {
        "fecha": "2024-03-10",
        "producto": _PRODUCTO,
        "cantidad": "2",
        "monto": "2100",
    }
    if context_id is not None:
        fila["__context__"] = context_id
    return fila


def _ctx(context_id: str, label: str) -> dict[str, Any]:
    return {
        "context_id": context_id,
        "label": label,
        "source_kind": "sheet",
        "entity_type": "sale",
        "headers": ["fecha", "producto", "cantidad", "monto"],
        "fields": None,
        "preview_rows": [],
        "row_count": 1,
    }


async def _crear_producto(db: AsyncSession, tenant: Tenant, stock: int = 10) -> Product:
    """El producto tiene que existir: una venta sólo entra a la proyección si resuelve."""
    producto = Product(
        id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        name=_PRODUCTO,
        sale_price_ars=Decimal("1050"),
        unit_cost_ars=Decimal("600"),
        stock_units=stock,
    )
    db.add(producto)
    await db.flush()
    return producto


async def _venta_unica(db: AsyncSession) -> SaleEntry:
    ventas = (await db.execute(select(SaleEntry))).scalars().all()
    assert len(ventas) == 1, f"se esperaba una venta, hay {len(ventas)}"
    return ventas[0]


@pytest.mark.asyncio
class TestLaVentaGuardaSuHoja:
    async def test_multi_hoja_estampa_el_contexto(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        await _crear_producto(db_session, sample_tenant)
        summary = {
            "file_type": "spreadsheet",
            "inferred_type": "mixed",
            "multi_sheet": True,
            "has_venta": True,
            "row_count": 1,
            "ventas_detectadas": [_fila_venta(_VENTAS)],
            "mapping_contexts": [_ctx(_VENTAS, "Ventas")],
        }

        await importer.insert_confirmed_data(
            db_session,
            sample_tenant.tenant_id,
            summary,
            {"ventas": True},
            context_mappings={_VENTAS: _MAPPING_VENTAS},
            context_confirmed={_VENTAS: True},
            inventory_effect={_VENTAS: "informational"},
        )
        await db_session.flush()

        venta = await _venta_unica(db_session)
        assert venta.custom_fields.get(IMPORT_CONTEXT_FIELD) == _VENTAS

    async def test_una_sola_tabla_tambien_estampa_su_hoja(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """El archivo plano tiene UNA hoja, y la venta tiene que decir cuál es."""
        await _crear_producto(db_session, sample_tenant)
        summary = {
            "file_type": "spreadsheet",
            "inferred_type": "ventas",
            "has_venta": True,
            "row_count": 1,
            "ventas_detectadas": [_fila_venta()],
            "mapping_contexts": [_ctx(_TABLA, "Hoja 1")],
        }

        await importer.insert_confirmed_data(
            db_session,
            sample_tenant.tenant_id,
            summary,
            {"ventas": True},
            column_mappings=_MAPPING_VENTAS,
            inventory_effect={_TABLA: "informational"},
        )
        await db_session.flush()

        venta = await _venta_unica(db_session)
        assert venta.custom_fields.get(IMPORT_CONTEXT_FIELD) == _TABLA

    async def test_una_venta_manual_no_gana_la_clave(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Control: la clave marca procedencia de import, no es un campo de toda venta."""
        venta = SaleEntry(
            tenant_id=sample_tenant.tenant_id,
            amount=Decimal("2100"),
            quantity=2,
            transaction_date=importer.datetime(2024, 3, 10),
        )
        db_session.add(venta)
        await db_session.flush()

        assert IMPORT_CONTEXT_FIELD not in (venta.custom_fields or {})


@pytest.mark.asyncio
class TestElEfectoDeclaradoLlegaALaProyeccion:
    async def test_una_sola_tabla_honra_no_inventory(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """`no_inventory` en un archivo plano tiene que sacar la fila de la proyección.

        Es el caso que delata el bug: sin pasarle el contexto al registrador, la
        hoja caía al default y el producto aparecía igual en el impacto, con el
        usuario habiendo declarado lo contrario.
        """
        await _crear_producto(db_session, sample_tenant)
        summary = {
            "file_type": "spreadsheet",
            "inferred_type": "ventas",
            "has_venta": True,
            "row_count": 1,
            "ventas_detectadas": [_fila_venta()],
            "mapping_contexts": [_ctx(_TABLA, "Hoja 1")],
        }

        counts = await importer.insert_confirmed_data(
            db_session,
            sample_tenant.tenant_id,
            summary,
            {"ventas": True},
            column_mappings=_MAPPING_VENTAS,
            inventory_effect={_TABLA: "no_inventory"},
        )

        assert counts["ventas"] == 1
        assert counts["impacto_inventario"] == []

    async def test_control_sin_declaracion_la_fila_si_proyecta(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Control del anterior: con el default, la misma fila SÍ entra al impacto.

        Sin esto, "no aparece en el impacto" no probaría nada — podría no aparecer
        porque el producto no resolvió o porque la proyección no corre en este camino.
        """
        await _crear_producto(db_session, sample_tenant)
        summary = {
            "file_type": "spreadsheet",
            "inferred_type": "ventas",
            "has_venta": True,
            "row_count": 1,
            "ventas_detectadas": [_fila_venta()],
            "mapping_contexts": [_ctx(_TABLA, "Hoja 1")],
        }

        counts = await importer.insert_confirmed_data(
            db_session,
            sample_tenant.tenant_id,
            summary,
            {"ventas": True},
            column_mappings=_MAPPING_VENTAS,
            inventory_effect={_TABLA: "informational"},
        )

        assert [p["product_name"] for p in counts["impacto_inventario"]] == [_PRODUCTO]
        assert counts["impacto_inventario"][0]["vendidas"] == 2
