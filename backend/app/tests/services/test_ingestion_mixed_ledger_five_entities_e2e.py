"""F7c — E2E del libro mixto de 5 entidades en UN solo confirm.

F7c cableó el orden maestro→transacción (clientes/proveedores ANTES que
ventas/gastos, misma transacción del confirm) y la resolución de referencia
por fila; F7d agregó el desglose de contadores. Los tests existentes
(``test_ingestion_reference_resolution_f7c.py``,
``test_ingestion_f7d_master_surface.py``) cubren cada pieza por separado. Este
módulo cierra el gap end-to-end: un ÚNICO archivo con las 5 entidades a la vez
(clientes + proveedores + productos + ventas + gastos), ejercitando
``insert_confirmed_data`` (el mismo helper que usa ``POST /ingestion/.../
confirm``) tal como llegaría un libro real con varias hojas.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.application.services.ingestion_import_service as importer
from app.config.settings import get_settings
from app.persistence.models.customer import Customer
from app.persistence.models.product import Product
from app.persistence.models.supplier import Supplier
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry

_VALID_CUSTOMER_CUIT = "20-12345678-6"
_VALID_SUPPLIER_CUIL = "27-23456789-1"
_SUPPLIER_NAME = "Distribuidora Sur"


def _five_entity_ledger_summary() -> dict[str, Any]:
    """Libro mixto de 5 hojas en un solo archivo: Clientes, Proveedores,
    Productos, Ventas y Gastos, cada una con su ``entity_type`` en
    ``mapping_contexts`` y sus filas marcadas con ``__context__``."""
    return {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "mapping_contexts": [
            {
                "context_id": "sheet:Clientes",
                "entity_type": "customer",
                "source_kind": "sheet",
                "headers": ["nombre", "documento"],
                "row_count": 1,
            },
            {
                "context_id": "sheet:Proveedores",
                "entity_type": "supplier",
                "source_kind": "sheet",
                "headers": ["nombre", "cuil"],
                "row_count": 1,
            },
            {
                "context_id": "sheet:Productos",
                "entity_type": "product",
                "source_kind": "sheet",
                "headers": ["producto", "precio", "stock"],
                "row_count": 1,
            },
            {
                "context_id": "sheet:Ventas",
                "entity_type": "sale",
                "source_kind": "sheet",
                "headers": ["fecha", "valor", "doc_cliente"],
                "row_count": 2,
            },
            {
                "context_id": "sheet:Gastos",
                "entity_type": "expense",
                "source_kind": "sheet",
                "headers": ["fecha", "monto", "proveedor"],
                "row_count": 1,
            },
        ],
        "clientes_detectados": [
            {
                "nombre": "Juan Perez",
                "documento": _VALID_CUSTOMER_CUIT,
                "__context__": "sheet:Clientes",
            }
        ],
        "proveedores_detectados": [
            {
                "nombre": _SUPPLIER_NAME,
                "cuil": _VALID_SUPPLIER_CUIL,
                "__context__": "sheet:Proveedores",
            }
        ],
        "stock_detectado": [
            {
                "producto": "Yerba",
                "precio": "5000",
                "stock": "20",
                "__context__": "sheet:Productos",
            }
        ],
        "ventas_detectadas": [
            {
                # matchea al cliente creado por la hoja Clientes de este mismo archivo.
                "fecha": "2024-01-15",
                "valor": "5000",
                "doc_cliente": _VALID_CUSTOMER_CUIT,
                "__context__": "sheet:Ventas",
            },
            {
                # sin ninguna columna de cliente: venta de mostrador → anonymous.
                "fecha": "2024-01-16",
                "valor": "3000",
                "__context__": "sheet:Ventas",
            },
        ],
        "gastos_detectados": [
            {
                # referencia al proveedor creado por la hoja Proveedores (por nombre,
                # modo "legacy" default de SUPPLIER_REFERENCE_CREATION_MODE).
                "fecha": "2024-01-15",
                "monto": "2000",
                "proveedor": _SUPPLIER_NAME,
                "__context__": "sheet:Gastos",
            }
        ],
    }


def _five_entity_context_mappings() -> dict[str, dict[str, str]]:
    return {
        "sheet:Clientes": {"nombre": "name", "documento": "cuit"},
        "sheet:Proveedores": {"nombre": "name", "cuil": "cuil"},
        "sheet:Productos": {
            "producto": "name",
            "precio": "sale_price_ars",
            "stock": "stock_units",
        },
        "sheet:Ventas": {
            "fecha": "transaction_date",
            "valor": "amount",
            "doc_cliente": "customer_cuit",
        },
        "sheet:Gastos": {
            "fecha": "expense_date",
            "monto": "amount",
            "proveedor": "supplier_name",
        },
    }


async def test_confirm_unico_con_5_entidades_orden_maestro_transaccion_y_contadores(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """UN solo confirm con clientes+proveedores+productos+ventas+gastos: los
    maestros se crean ANTES y las transacciones vinculan a esas MISMAS
    entidades (no crean duplicados), todo en la transacción del confirm."""
    assert get_settings().SUPPLIER_REFERENCE_CREATION_MODE == "legacy"

    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        _five_entity_ledger_summary(),
        confirmed_fields={
            "clientes": True,
            "proveedores": True,
            "productos": True,
            "ventas": True,
            "gastos": True,
        },
        context_mappings=_five_entity_context_mappings(),
        context_confirmed={
            "sheet:Clientes": True,
            "sheet:Proveedores": True,
            "sheet:Productos": True,
            "sheet:Ventas": True,
            "sheet:Gastos": True,
        },
    )

    # ── Contadores F7d reflejan lo importado ────────────────────────────────
    assert counts["clientes_creados"] == 1
    assert counts["clientes"] == 1
    assert counts["proveedores_creados"] == 1
    assert counts["proveedores"] == 1
    assert counts["productos"] == 1
    assert counts["ventas"] == 2
    assert counts["ventas_cliente_identificado"] == 1
    assert counts["ventas_cliente_anonimo"] == 1
    assert counts["ventas_cliente_no_resuelto"] == 0
    assert counts["gastos"] == 1
    # La compra matcheó al proveedor recién creado: nunca cayó al sentinela.
    assert counts["sin_proveedor"] == 0

    # ── Orden maestro→transacción: los maestros existen (uno solo cada uno) ──
    customer = (
        await db_session.execute(select(Customer).where(Customer.name == "Juan Perez"))
    ).scalar_one()
    assert customer.cuit == _VALID_CUSTOMER_CUIT

    supplier = (
        await db_session.execute(select(Supplier).where(Supplier.name == _SUPPLIER_NAME))
    ).scalar_one()
    assert supplier.cuil == _VALID_SUPPLIER_CUIL

    product = (
        await db_session.execute(select(Product).where(Product.name == "Yerba"))
    ).scalar_one()
    assert product.sale_price_ars == Decimal("5000")
    assert product.stock_units == 20

    # ── La venta matched vinculó al cliente recién creado (no creó otro) ────
    sales = (
        (await db_session.execute(select(SaleEntry).order_by(SaleEntry.amount)))
        .scalars()
        .all()
    )
    assert len(sales) == 2
    anonymous_sale, matched_sale = sales  # amount 3000, 5000 respectivamente
    assert matched_sale.customer_id == customer.id
    assert matched_sale.custom_fields is not None
    assert matched_sale.custom_fields["_customer_resolution"] == "matched"

    # ── La venta anónima cayó a "Local", SIN warning (ni traza de "raw") ────
    local = (
        await db_session.execute(select(Customer).where(Customer.name == "Local"))
    ).scalar_one()
    assert anonymous_sale.customer_id == local.id
    assert anonymous_sale.custom_fields is not None
    assert anonymous_sale.custom_fields["_customer_resolution"] == "anonymous"
    assert "_customer_reference_raw" not in anonymous_sale.custom_fields

    # Ningún cliente de más: solo el creado por la hoja + el sentinela "Local".
    customer_names = {c.name for c in (await db_session.execute(select(Customer))).scalars().all()}
    assert customer_names == {"Juan Perez", "Local"}

    # ── La compra vinculó al MISMO proveedor recién creado (no duplicó) ─────
    expense = (await db_session.execute(select(ExpenseEntry))).scalar_one()
    assert expense.supplier_id == supplier.id

    suppliers = (await db_session.execute(select(Supplier))).scalars().all()
    assert len(suppliers) == 1  # sin sentinela "No identificado" ni duplicado

    # Modo legacy (default): sin traza de resolución de proveedor — no cambia
    # el comportamiento preexistente (ver test_compra_modo_legacy... en
    # test_ingestion_reference_resolution_f7c.py).
    assert not (expense.custom_fields or {}).get("_supplier_resolution")
