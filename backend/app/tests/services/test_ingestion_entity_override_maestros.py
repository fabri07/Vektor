"""Reasignar a mano la SECCIÓN de una hoja mal clasificada, incluidos maestros.

El clasificador se equivoca: en un archivo real mandó a "Productos" una hoja
llamada Clientes (9 filas) y otra llamada Ventas (1187). El override por
contexto (``context_entity``) ya funcionaba para ventas/gastos/productos —lee
las filas del bucket del tipo ORIGINAL—, pero ``_import_master_entities``
resolvía la entidad leyendo ``entity_type`` del summary e iba a buscar las
filas a ``clientes_detectados``. Una hoja que el parser mandó a productos no
las tiene ahí, así que reasignarla a Clientes confirmaba sin error y no
importaba nada.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.application.services.ingestion_import_service as importer
from app.persistence.models.customer import Customer
from app.persistence.models.product import Product
from app.persistence.models.supplier import Supplier
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry


def _hoja_de_clientes_clasificada_como_productos() -> dict[str, Any]:
    """Lo de la captura: la hoja se llama "Clientes" y el parser dijo product."""
    return {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "mapping_contexts": [
            {
                "context_id": "sheet:Clientes",
                "label": "Clientes",
                "entity_type": "product",  # el parser se equivocó
                "source_kind": "sheet",
                "headers": ["Nombre (correcto)", "Documento"],
                "fields": None,
                "preview_rows": [],
                "row_count": 2,
            },
        ],
        # Las filas viven donde las dejó el parser: el bucket de productos.
        "stock_detectado": [
            {
                "Nombre (correcto)": "Juan Perez",
                "Documento": "30123456",
                "__context__": "sheet:Clientes",
            },
            {
                "Nombre (correcto)": "Ana Gomez",
                "Documento": "40987654",
                "__context__": "sheet:Clientes",
            },
        ],
        "ventas_detectadas": [],
        "gastos_detectados": [],
        "clientes_detectados": [],
        "proveedores_detectados": [],
    }


@pytest.mark.asyncio
async def test_hoja_de_clientes_mal_clasificada_se_importa_como_clientes(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        _hoja_de_clientes_clasificada_como_productos(),
        {"clientes": True},
        context_mappings={
            "sheet:Clientes": {"Nombre (correcto)": "name", "Documento": "dni"},
        },
        context_confirmed={"sheet:Clientes": True},
        context_entity={"sheet:Clientes": "customer"},
    )

    assert counts["clientes"] == 2
    nombres = sorted(
        c.name for c in (await db_session.execute(select(Customer))).scalars().all()
    )
    assert nombres == ["Ana Gomez", "Juan Perez"]

    # Y NO se crearon productos con los nombres de los clientes.
    assert (await db_session.execute(select(Product))).scalars().all() == []


@pytest.mark.asyncio
async def test_sin_override_la_hoja_sigue_yendo_a_productos(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Control: el reconocimiento automático sigue vigente cuando nadie lo corrige."""
    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        _hoja_de_clientes_clasificada_como_productos(),
        {"productos": True},
        context_mappings={"sheet:Clientes": {"Nombre (correcto)": "name"}},
        context_confirmed={"sheet:Clientes": True},
    )

    assert counts["productos"] == 2
    assert (await db_session.execute(select(Customer))).scalars().all() == []


@pytest.mark.asyncio
async def test_hoja_de_proveedores_mal_clasificada_se_importa_como_proveedores(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    # El import de proveedores necesita una clave fuerte (CUIL/email/teléfono):
    # sin ella la fila queda en needs_review por diseño, y este test es sobre el
    # ruteo de la hoja, no sobre esa regla.
    summary: dict[str, Any] = {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "mapping_contexts": [
            {
                "context_id": "sheet:Proveedores",
                "label": "Proveedores",
                "entity_type": "product",  # el parser se equivocó
                "source_kind": "sheet",
                "headers": ["Razon social", "Mail"],
                "fields": None,
                "preview_rows": [],
                "row_count": 2,
            },
        ],
        "stock_detectado": [
            {
                "Razon social": "Distribuidora Sur",
                "Mail": "ventas@sur.com.ar",
                "__context__": "sheet:Proveedores",
            },
            {
                "Razon social": "Quimica Norte",
                "Mail": "info@norte.com.ar",
                "__context__": "sheet:Proveedores",
            },
        ],
        "ventas_detectadas": [],
        "gastos_detectados": [],
        "clientes_detectados": [],
        "proveedores_detectados": [],
    }

    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        {"proveedores": True},
        context_mappings={
            "sheet:Proveedores": {"Razon social": "name", "Mail": "email"},
        },
        context_confirmed={"sheet:Proveedores": True},
        context_entity={"sheet:Proveedores": "supplier"},
    )

    assert counts["proveedores"] == 2
    assert (await db_session.execute(select(Product))).scalars().all() == []


@pytest.mark.asyncio
async def test_hoja_de_clientes_reasignada_a_ventas_encuentra_sus_filas(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """El espejo: el parser dijo "clientes" y en realidad eran ventas.

    Las filas están en ``clientes_detectados``; el dispatch transaccional tiene
    que ir a buscarlas ahí, no a ``otros_detectados``.
    """
    summary: dict[str, Any] = {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "mapping_contexts": [
            {
                "context_id": "sheet:Movimientos",
                "label": "Movimientos",
                "entity_type": "customer",  # el parser se equivocó
                "source_kind": "sheet",
                "headers": ["fecha", "valor"],
                "fields": None,
                "preview_rows": [],
                "row_count": 1,
            },
        ],
        "clientes_detectados": [
            {"fecha": "2024-01-15", "valor": "5000", "__context__": "sheet:Movimientos"}
        ],
        "ventas_detectadas": [],
        "gastos_detectados": [],
        "stock_detectado": [],
    }

    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        {"ventas": True},
        context_mappings={
            "sheet:Movimientos": {"valor": "amount", "fecha": "transaction_date"},
        },
        context_confirmed={"sheet:Movimientos": True},
        context_entity={"sheet:Movimientos": "sale"},
    )

    assert counts["ventas"] == 1
    assert (await db_session.execute(select(Supplier))).scalars().all() == []
    sale = (await db_session.execute(select(SaleEntry))).scalar_one()
    assert sale.amount == 5000
