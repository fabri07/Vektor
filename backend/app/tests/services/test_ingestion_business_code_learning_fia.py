"""F-I(A) — una fila que matchea por documento/email/teléfono y también trae
un `business_code` lo persiste en `entity_identifiers` en vez de descartarlo
después de usarlo una sola vez (`_record_row_business_code`,
`ingestion_import_service.py`).

Complementa F-ID.7 (`test_ingestion_reference_resolution_by_code_fid7.py`,
sin tocar): ese archivo prueba que un código YA registrado resuelve: este
prueba que un código NUEVO que llega junto a un match por documento se
aprende — con same-file learning (una fila posterior del mismo archivo lo
reconoce sin bootstrap) y sin nunca vincular a ciegas ante un conflicto real.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.application.services.ingestion_import_service as importer
from app.application.services.entity_code_service import record_identifier
from app.persistence.models.customer import Customer
from app.persistence.models.entity_identifier import EntityIdentifier
from app.persistence.models.supplier import Supplier
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.repositories.customer_repository import CustomerRepository
from app.persistence.repositories.supplier_repository import SupplierRepository


async def test_match_por_documento_con_business_code_nuevo_se_registra(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    repo = CustomerRepository(db_session)
    customer = await repo.save(
        Customer(tenant_id=sample_tenant.tenant_id, name="Cliente Uno", dni="30111222")
    )

    summary: dict[str, Any] = {
        "file_type": "spreadsheet",
        "inferred_type": "ventas",
        "has_venta": True,
        "ventas_detectadas": [
            {
                "fecha": "2024-01-15",
                "monto": "3000",
                "doc_cliente": "30111222",
                "codigo_cliente": "ERP-77",
            }
        ],
    }
    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        {"ventas": True},
        column_mappings={
            "doc_cliente": "customer_dni",
            "codigo_cliente": "customer_business_code",
        },
    )

    assert counts["ventas_cliente_identificado"] == 1
    sale = (await db_session.execute(select(SaleEntry))).scalar_one()
    assert sale.customer_id == customer.id

    identifiers = (
        await db_session.execute(
            select(EntityIdentifier).where(
                EntityIdentifier.tenant_id == sample_tenant.tenant_id,
                EntityIdentifier.entity_type == "customer",
                EntityIdentifier.identifier_type == "business_code",
            )
        )
    ).scalars().all()
    assert len(identifiers) == 1
    assert identifiers[0].entity_id == customer.id
    assert identifiers[0].normalized_value == "erp-77"
    assert identifiers[0].namespace == "business"
    assert identifiers[0].origin == "business"


async def test_fila_2_del_mismo_archivo_resuelve_por_el_codigo_que_aprendio_la_fila_1(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Same-file learning: sin este mecanismo, la fila 2 (solo trae el código,
    sin documento) sólo resolvería en la PRÓXIMA importación."""
    repo = CustomerRepository(db_session)
    customer = await repo.save(
        Customer(tenant_id=sample_tenant.tenant_id, name="Cliente Uno", dni="30111222")
    )

    summary: dict[str, Any] = {
        "file_type": "spreadsheet",
        "inferred_type": "ventas",
        "has_venta": True,
        "ventas_detectadas": [
            {
                "fecha": "2024-01-15",
                "monto": "3000",
                "doc_cliente": "30111222",
                "codigo_cliente": "ERP-77",
            },
            {
                "fecha": "2024-01-16",
                "monto": "1500",
                "codigo_cliente": "ERP-77",
            },
        ],
    }
    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        {"ventas": True},
        column_mappings={
            "doc_cliente": "customer_dni",
            "codigo_cliente": "customer_business_code",
        },
    )

    assert counts["ventas_cliente_identificado"] == 2
    sales_result = await db_session.execute(select(SaleEntry))
    sales = sales_result.scalars().all()
    assert {s.customer_id for s in sales} == {customer.id}


async def test_business_code_ya_de_otra_entidad_degrada_a_unresolved(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """El código ya pertenece a un cliente DESACTIVADO (fuera del índice que
    arma `_load_customer_identity_index`, que sólo trae activos) — el intento
    de registrarlo para el cliente que matcheó por documento choca contra
    `entity_identifiers` en la escritura, no en la lectura. La fila no puede
    importarse vinculada a ciegas: cae a unresolved."""
    repo = CustomerRepository(db_session)
    other = await repo.save(Customer(tenant_id=sample_tenant.tenant_id, name="Cliente Viejo"))
    await record_identifier(
        db_session,
        sample_tenant.tenant_id,
        "customer",
        other.id,
        "business_code",
        "business",
        "ERP-77",
        "business",
    )
    other.deactivated_at = other.created_at
    await db_session.commit()

    customer = await repo.save(
        Customer(tenant_id=sample_tenant.tenant_id, name="Cliente Nuevo", dni="30111222")
    )

    summary: dict[str, Any] = {
        "file_type": "spreadsheet",
        "inferred_type": "ventas",
        "has_venta": True,
        "ventas_detectadas": [
            {
                "fecha": "2024-01-15",
                "monto": "3000",
                "doc_cliente": "30111222",
                "codigo_cliente": "ERP-77",
            }
        ],
    }
    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        {"ventas": True},
        column_mappings={
            "doc_cliente": "customer_dni",
            "codigo_cliente": "customer_business_code",
        },
    )

    assert counts["ventas_cliente_no_resuelto"] == 1
    assert counts.get("clientes_referencia_conflictiva") == 1
    sale = (await db_session.execute(select(SaleEntry))).scalar_one()
    assert sale.custom_fields["_customer_resolution"] == "unresolved"
    assert sale.customer_id not in (customer.id, other.id)

    # El identificador conflictivo sigue perteneciendo a la entidad original —
    # nunca se reasigna en silencio.
    identifiers = (
        await db_session.execute(
            select(EntityIdentifier).where(
                EntityIdentifier.tenant_id == sample_tenant.tenant_id,
                EntityIdentifier.identifier_type == "business_code",
                EntityIdentifier.normalized_value == "erp-77",
            )
        )
    ).scalars().all()
    assert len(identifiers) == 1
    assert identifiers[0].entity_id == other.id


async def test_relectura_no_duplica_el_identificador(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Importar el mismo archivo dos veces (simulando una relectura) no crea
    una segunda fila en `entity_identifiers` — `record_identifier` es
    idempotente por valor normalizado, sólo actualiza `last_seen_at`."""
    repo = CustomerRepository(db_session)
    customer = await repo.save(
        Customer(tenant_id=sample_tenant.tenant_id, name="Cliente Uno", dni="30111222")
    )

    def _summary() -> dict[str, Any]:
        return {
            "file_type": "spreadsheet",
            "inferred_type": "ventas",
            "has_venta": True,
            "ventas_detectadas": [
                {
                    "fecha": "2024-01-15",
                    "monto": "3000",
                    "doc_cliente": "30111222",
                    "codigo_cliente": "ERP-77",
                }
            ],
        }

    mappings = {
        "doc_cliente": "customer_dni",
        "codigo_cliente": "customer_business_code",
    }
    await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, _summary(), {"ventas": True}, column_mappings=mappings
    )
    await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, _summary(), {"ventas": True}, column_mappings=mappings
    )

    identifiers = (
        await db_session.execute(
            select(EntityIdentifier).where(
                EntityIdentifier.tenant_id == sample_tenant.tenant_id,
                EntityIdentifier.identifier_type == "business_code",
                EntityIdentifier.normalized_value == "erp-77",
            )
        )
    ).scalars().all()
    assert len(identifiers) == 1
    assert identifiers[0].entity_id == customer.id


async def test_compra_link_only_con_proveedor_por_documento_aprende_business_code(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: Any
) -> None:
    """Mismo mecanismo, lado proveedor + camino de gastos (link_only)."""
    from app.config.settings import get_settings

    monkeypatch.setattr(get_settings(), "SUPPLIER_REFERENCE_CREATION_MODE", "link_only")
    repo = SupplierRepository(db_session)
    supplier = await repo.save(
        Supplier(tenant_id=sample_tenant.tenant_id, name="Distribuidora SA", cuil="20111222339")
    )

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
                "cuil_proveedor": "20111222339",
                "codigo_proveedor": "ERP-SUP-1",
            }
        ],
    }
    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        {"gastos": True},
        column_mappings={
            "cuil_proveedor": "supplier_cuil",
            "codigo_proveedor": "supplier_business_code",
        },
    )

    assert counts["compras_proveedor_identificado"] == 1
    identifiers = (
        await db_session.execute(
            select(EntityIdentifier).where(
                EntityIdentifier.tenant_id == sample_tenant.tenant_id,
                EntityIdentifier.entity_type == "supplier",
                EntityIdentifier.identifier_type == "business_code",
            )
        )
    ).scalars().all()
    assert len(identifiers) == 1
    assert identifiers[0].entity_id == supplier.id
