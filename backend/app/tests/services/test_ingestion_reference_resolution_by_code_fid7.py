"""F-ID.7 — una venta/gasto con código externo resuelve al cliente/proveedor.

Extiende F7c (`test_ingestion_reference_resolution_f7c.py`, sin tocar) con un
nuevo tier de más prioridad que documento: el código (`vektor_code` de la
entidad, o un `business_code` ya registrado en `entity_identifiers` — p. ej.
por `scripts/bootstrap_entity_identifiers.py` o un import anterior). El
código gana sobre documento/nombre — una variante de nombre nunca lo
contradice, y dos identificadores fuertes que apuntan a entidades distintas
dan conflict (unresolved), nunca "gana el primero".
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.application.services.ingestion_import_service as importer
from app.application.services.entity_code_service import record_identifier
from app.config.settings import get_settings
from app.persistence.models.customer import Customer
from app.persistence.models.supplier import Supplier
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.persistence.repositories.customer_repository import CustomerRepository
from app.persistence.repositories.supplier_repository import SupplierRepository


async def test_venta_con_vektor_code_resuelve_aunque_el_nombre_no_coincida(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    repo = CustomerRepository(db_session)
    customer = await repo.save(Customer(tenant_id=sample_tenant.tenant_id, name="Juan Pérez"))
    assert customer.vektor_code == "CLI-0001"

    summary: dict[str, Any] = {
        "file_type": "spreadsheet",
        "inferred_type": "ventas",
        "has_venta": True,
        "ventas_detectadas": [
            {
                "fecha": "2024-01-15",
                "monto": "3000",
                "codigo_cliente": "CLI-0001",
                "nombre_distinto": "Alguien Que No Es Juan",
            }
        ],
    }
    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        {"ventas": True},
        column_mappings={
            "codigo_cliente": "customer_business_code",
            "nombre_distinto": "customer_name",
        },
    )

    assert counts["ventas_cliente_identificado"] == 1
    sale = (await db_session.execute(select(SaleEntry))).scalar_one()
    assert sale.customer_id == customer.id
    assert sale.custom_fields["_customer_resolution"] == "matched"


async def test_venta_con_business_code_registrado_resuelve(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """El código no tiene que ser el vektor_code propio — un `business_code`
    ya registrado en entity_identifiers (p. ej. por el bootstrap de F-ID.4)
    también resuelve."""
    customer = Customer(tenant_id=sample_tenant.tenant_id, name="Almacén Doña Rosa")
    db_session.add(customer)
    await db_session.flush()
    await record_identifier(
        db_session,
        sample_tenant.tenant_id,
        "customer",
        customer.id,
        "business_code",
        "business",
        "ERP-918",
        "business",
    )
    await db_session.commit()

    summary: dict[str, Any] = {
        "file_type": "spreadsheet",
        "inferred_type": "ventas",
        "has_venta": True,
        "ventas_detectadas": [
            {"fecha": "2024-01-15", "monto": "3000", "codigo_cliente": "erp-918"}
        ],
    }
    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        {"ventas": True},
        column_mappings={"codigo_cliente": "customer_business_code"},
    )

    assert counts["ventas_cliente_identificado"] == 1
    sale = (await db_session.execute(select(SaleEntry))).scalar_one()
    assert sale.customer_id == customer.id


async def test_venta_sin_columna_de_codigo_no_cambia_comportamiento(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """No-regresión: un archivo que no mapea customer_business_code sigue
    resolviendo por documento exactamente igual que antes de F-ID.7."""
    customer = Customer(tenant_id=sample_tenant.tenant_id, name="Juan Pérez", dni="30123456")
    db_session.add(customer)
    await db_session.commit()

    summary: dict[str, Any] = {
        "file_type": "spreadsheet",
        "inferred_type": "ventas",
        "has_venta": True,
        "ventas_detectadas": [
            {"fecha": "2024-01-15", "monto": "3000", "doc_cliente": "30123456"}
        ],
    }
    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        {"ventas": True},
        column_mappings={"doc_cliente": "customer_dni"},
    )

    assert counts["ventas_cliente_identificado"] == 1
    sale = (await db_session.execute(select(SaleEntry))).scalar_one()
    assert sale.customer_id == customer.id


async def test_codigo_y_documento_contradictorios_dan_unresolved(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Dos identificadores fuertes de la MISMA fila apuntando a clientes
    DISTINTOS: conflict → unresolved, nunca gana el código en silencio."""
    repo = CustomerRepository(db_session)
    a = await repo.save(Customer(tenant_id=sample_tenant.tenant_id, name="Cliente A"))
    b = Customer(tenant_id=sample_tenant.tenant_id, name="Cliente B", dni="30999888")
    db_session.add(b)
    await db_session.commit()
    assert a.vektor_code == "CLI-0001"

    summary: dict[str, Any] = {
        "file_type": "spreadsheet",
        "inferred_type": "ventas",
        "has_venta": True,
        "ventas_detectadas": [
            {
                "fecha": "2024-01-15",
                "monto": "3000",
                "codigo_cliente": "CLI-0001",
                "doc_cliente": "30999888",
            }
        ],
    }
    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        {"ventas": True},
        column_mappings={
            "codigo_cliente": "customer_business_code",
            "doc_cliente": "customer_dni",
        },
    )

    assert counts["ventas_cliente_no_resuelto"] == 1
    sale = (await db_session.execute(select(SaleEntry))).scalar_one()
    assert sale.custom_fields["_customer_resolution"] == "unresolved"
    # Nunca crea; ambos clientes reales quedan intocados.
    assert sale.customer_id not in (a.id, b.id)


async def test_compra_link_only_matchea_proveedor_por_vektor_code(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "SUPPLIER_REFERENCE_CREATION_MODE", "link_only")
    repo = SupplierRepository(db_session)
    supplier = await repo.save(Supplier(tenant_id=sample_tenant.tenant_id, name="Distribuidora SA"))
    assert supplier.vektor_code == "PRV-0001"

    summary: dict[str, Any] = {
        "file_type": "spreadsheet",
        "inferred_type": "gastos",
        "has_gasto": True,
        "gastos_detectados": [
            {
                "fecha": "2024-01-15",
                "gasto": "5000",
                "producto": "Yerba",
                "cantidad": "10",
                "codigo_proveedor": "PRV-0001",
            }
        ],
    }
    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        {"gastos": True},
        column_mappings={"codigo_proveedor": "supplier_business_code"},
    )

    assert counts["compras_proveedor_identificado"] == 1
    expense = (await db_session.execute(select(ExpenseEntry))).scalar_one()
    assert expense.supplier_id == supplier.id


async def test_venta_con_codigo_resuelve_tambien_en_el_camino_multi_hoja(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Mismo mecanismo que el primer test, pero por el camino multi-hoja
    (`mapping_contexts`) — es una copia paralela del resolvedor en
    `ingestion_import_service.py`, no alcanza con probar un solo camino."""
    repo = CustomerRepository(db_session)
    customer = await repo.save(Customer(tenant_id=sample_tenant.tenant_id, name="Juan Pérez"))
    assert customer.vektor_code == "CLI-0001"

    summary: dict[str, Any] = {
        "file_type": "spreadsheet",
        "multi_sheet": True,
        "mapping_contexts": [
            {
                "context_id": "sheet:Ventas",
                "entity_type": "sale",
                "source_kind": "sheet",
                "headers": ["fecha", "valor", "codigo_cliente"],
                "fields": None,
                "preview_rows": [],
                "row_count": 1,
            },
        ],
        "ventas_detectadas": [
            {
                "fecha": "2024-01-15",
                "valor": "3000",
                "codigo_cliente": "CLI-0001",
                "__context__": "sheet:Ventas",
            }
        ],
        "gastos_detectados": [],
        "stock_detectado": [],
        "clientes_detectados": [],
    }
    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        {"ventas": True},
        context_mappings={
            "sheet:Ventas": {
                "valor": "amount",
                "fecha": "transaction_date",
                "codigo_cliente": "customer_business_code",
            }
        },
        context_confirmed={"sheet:Ventas": True},
    )

    assert counts["ventas_cliente_identificado"] == 1
    sale = (await db_session.execute(select(SaleEntry))).scalar_one()
    assert sale.customer_id == customer.id


def test_field_catalog_expone_business_code() -> None:
    from app.application.services.column_mapping_service import CANONICAL_FIELDS

    assert "customer_business_code" in CANONICAL_FIELDS["sale"]
    assert "supplier_business_code" in CANONICAL_FIELDS["expense"]
