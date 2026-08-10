"""F7c — orden maestro→transacción + resolución de cliente/proveedor por fila.

7a abrió los campos de referencia (``customer_dni``/``customer_cuit``/...,
``supplier_cuil``/...) en ``column_mapping_service``; 7b construyó el motor de
identidad común (``identity_resolution``) + los import services de maestro. Este
módulo cablea ambos: los maestros se importan ANTES que cualquier venta/gasto
(misma transacción del confirm), y cada fila transaccional resuelve su
referencia contra el índice de identidad — nunca crea una entidad desde una fila.

Cubre:
  - orden maestro→transacción (venta matchea a un cliente creado por otra hoja
    del MISMO archivo);
  - semántica de 3 vías en ventas (matched/anonymous/unresolved), con la regla
    "sin columna de cliente → anonymous, SIN warning";
  - espejo en compras, gobernado por ``SUPPLIER_REFERENCE_CREATION_MODE``
    ("legacy" no cambia nada; "link_only" nunca crea);
  - rollback integral (todo en la misma transacción del confirm).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.application.services.ingestion_import_service as importer
from app.config.settings import get_settings
from app.persistence.models.customer import Customer
from app.persistence.models.supplier import Supplier
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry

_VALID_DNI = "30123456"
_OTHER_DNI = "40987654"


def _mixed_customer_sale_summary() -> dict[str, Any]:
    """Libro mixto: hoja "Clientes" + hoja "Ventas" que referencia esos clientes
    por documento (mismo archivo, mismo confirm)."""
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
                "fields": None,
                "preview_rows": [],
                "row_count": 1,
            },
            {
                "context_id": "sheet:Ventas",
                "entity_type": "sale",
                "source_kind": "sheet",
                "headers": ["fecha", "valor", "doc_cliente"],
                "fields": None,
                "preview_rows": [],
                "row_count": 1,
            },
        ],
        "clientes_detectados": [
            {"nombre": "Juan Perez", "documento": _VALID_DNI, "__context__": "sheet:Clientes"}
        ],
        "ventas_detectadas": [
            {
                "fecha": "2024-01-15",
                "valor": "5000",
                "doc_cliente": _VALID_DNI,
                "__context__": "sheet:Ventas",
            }
        ],
        "gastos_detectados": [],
        "stock_detectado": [],
    }


# ── 1. Orden maestro→transacción ───────────────────────────────────────────────


async def test_venta_matchea_cliente_creado_por_otra_hoja_del_mismo_archivo(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        _mixed_customer_sale_summary(),
        {"ventas": True, "clientes": True},
        context_mappings={
            "sheet:Clientes": {"nombre": "name", "documento": "dni"},
            "sheet:Ventas": {
                "valor": "amount",
                "fecha": "transaction_date",
                "doc_cliente": "customer_dni",
            },
        },
        context_confirmed={"sheet:Clientes": True, "sheet:Ventas": True},
    )
    assert counts["clientes"] == 1
    assert counts["ventas"] == 1
    assert counts["ventas_cliente_identificado"] == 1
    assert counts["ventas_cliente_no_resuelto"] == 0

    customer = (await db_session.execute(select(Customer))).scalar_one()
    assert customer.name == "Juan Perez"
    assert customer.dni == _VALID_DNI

    sale = (await db_session.execute(select(SaleEntry))).scalar_one()
    assert sale.customer_id == customer.id
    assert sale.custom_fields is not None
    assert sale.custom_fields["_customer_resolution"] == "matched"


# ── 2. Venta sin columna de cliente → anonymous, SIN warning ──────────────────


async def test_venta_sin_columna_de_cliente_es_anonymous_sin_marcar_para_revision(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Venta de mostrador (sin ninguna columna customer_*): Local, SIN contar
    como "sin identificar" — es el caso normal, no amerita revisión."""
    summary: dict[str, Any] = {
        "file_type": "spreadsheet",
        "inferred_type": "ventas",
        "has_venta": True,
        "ventas_detectadas": [{"fecha": "2024-01-15", "monto": "3000"}],
    }
    counts = await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"ventas": True}
    )
    assert counts["ventas"] == 1
    assert counts["ventas_cliente_no_resuelto"] == 0
    assert counts["ventas_cliente_anonimo"] == 1
    assert counts["ventas_cliente_identificado"] == 0

    sale = (await db_session.execute(select(SaleEntry))).scalar_one()
    assert sale.custom_fields is not None
    assert sale.custom_fields["_customer_resolution"] == "anonymous"
    assert "_customer_reference_raw" not in sale.custom_fields

    local = (await db_session.execute(select(Customer))).scalar_one()
    assert local.name == "Local"
    assert sale.customer_id == local.id


# ── 3. Venta con doc de cliente inexistente → unresolved + traza + warning ────


async def test_venta_con_documento_de_cliente_inexistente_es_unresolved(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    summary: dict[str, Any] = {
        "file_type": "spreadsheet",
        "inferred_type": "ventas",
        "has_venta": True,
        "ventas_detectadas": [
            {"fecha": "2024-01-15", "monto": "3000", "doc_cliente": _OTHER_DNI}
        ],
    }
    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        {"ventas": True},
        column_mappings={"doc_cliente": "customer_dni"},
    )
    assert counts["ventas"] == 1
    assert counts["ventas_cliente_no_resuelto"] == 1
    assert counts["ventas_cliente_anonimo"] == 0

    sale = (await db_session.execute(select(SaleEntry))).scalar_one()
    assert sale.custom_fields is not None
    assert sale.custom_fields["_customer_resolution"] == "unresolved"
    assert sale.custom_fields["_customer_reference_raw"] == _OTHER_DNI

    # Nunca crea un cliente desde la fila: solo existe el sentinela "Local".
    customers = (await db_session.execute(select(Customer))).scalars().all()
    assert len(customers) == 1
    assert customers[0].name == "Local"
    assert sale.customer_id == customers[0].id


# ── 4. Compras: legacy no cambia nada / link_only nunca crea ──────────────────


def _merch_purchase_summary(supplier_name: str) -> dict[str, Any]:
    return {
        "file_type": "spreadsheet",
        "inferred_type": "gastos",
        "has_gasto": True,
        "gastos_detectados": [
            {
                "fecha": "2024-01-15",
                "gasto": "5000",
                "producto": "Yerba",
                "cantidad": "10",
                "proveedor": supplier_name,
            }
        ],
    }


async def test_compra_modo_legacy_sigue_creando_proveedor_por_nombre(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """No-regresión: SUPPLIER_REFERENCE_CREATION_MODE default "legacy" — el
    comportamiento de compras NO cambia (find-or-create por nombre, como hoy)."""
    assert get_settings().SUPPLIER_REFERENCE_CREATION_MODE == "legacy"
    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        _merch_purchase_summary("Distribuidora Norte"),
        {"gastos": True},
    )
    assert counts["compras_proveedor_no_resuelto"] == 0
    assert counts["compras_proveedor_anonimo"] == 0
    assert counts["compras_proveedor_identificado"] == 0

    supplier = (await db_session.execute(select(Supplier))).scalar_one()
    assert supplier.name == "Distribuidora Norte"
    expense = (await db_session.execute(select(ExpenseEntry))).scalar_one()
    assert expense.supplier_id == supplier.id
    # Modo legacy: sin traza de resolución (comportamiento sin cambios).
    assert not (expense.custom_fields or {}).get("_supplier_resolution")


async def test_compra_modo_link_only_nunca_crea_proveedor_cae_a_sentinela(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "SUPPLIER_REFERENCE_CREATION_MODE", "link_only")
    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        _merch_purchase_summary("Distribuidora Fantasma"),
        {"gastos": True},
    )
    assert counts["compras_proveedor_no_resuelto"] == 1
    assert counts["compras_proveedor_identificado"] == 0

    # NUNCA crea un proveedor real desde la fila: solo el sentinela.
    suppliers = (await db_session.execute(select(Supplier))).scalars().all()
    assert len(suppliers) == 1
    assert suppliers[0].name == "No identificado"

    expense = (await db_session.execute(select(ExpenseEntry))).scalar_one()
    assert expense.supplier_id == suppliers[0].id
    assert expense.custom_fields is not None
    assert expense.custom_fields["_supplier_resolution"] == "unresolved"
    assert expense.custom_fields["_supplier_reference_raw"] == "Distribuidora Fantasma"


async def test_compra_modo_link_only_matchea_proveedor_existente_por_cuil(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "SUPPLIER_REFERENCE_CREATION_MODE", "link_only")
    valid_cuil = "20-12345678-6"
    existing = Supplier(
        id=uuid.uuid4(),
        tenant_id=sample_tenant.tenant_id,
        name="Distribuidora Real SA",
        cuil=valid_cuil,
    )
    db_session.add(existing)
    await db_session.flush()

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
                "proveedor": "Distribuidora Real",  # nombre distinto a propósito
                "cuil_prov": valid_cuil,
            }
        ],
    }
    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        {"gastos": True},
        column_mappings={"cuil_prov": "supplier_cuil"},
    )
    assert counts["compras_proveedor_no_resuelto"] == 0
    assert counts["compras_proveedor_identificado"] == 1

    suppliers = (await db_session.execute(select(Supplier))).scalars().all()
    assert len(suppliers) == 1  # matcheó el existente, no creó uno nuevo
    expense = (await db_session.execute(select(ExpenseEntry))).scalar_one()
    assert expense.supplier_id == existing.id
    assert expense.custom_fields is not None
    assert expense.custom_fields["_supplier_resolution"] == "matched"


async def test_compra_link_only_opex_sin_referencia_no_usa_sentinela(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El sentinela NUNCA se aplica a OPEX (gasto sin producto) — invariante
    preexistente que link_only debe respetar igual que legacy."""
    monkeypatch.setattr(get_settings(), "SUPPLIER_REFERENCE_CREATION_MODE", "link_only")
    summary: dict[str, Any] = {
        "file_type": "spreadsheet",
        "inferred_type": "gastos",
        "has_gasto": True,
        "gastos_detectados": [
            {"fecha": "2024-01-15", "gasto": "5000", "categoria": "alquiler"}
        ],
    }
    await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"gastos": True}
    )
    assert (await db_session.execute(select(Supplier))).first() is None
    expense = (await db_session.execute(select(ExpenseEntry))).scalar_one()
    assert expense.supplier_id is None


# ── 5. Rollback integral ────────────────────────────────────────────────────────


async def test_rollback_integral_si_falla_una_etapa_no_persiste_nada(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Si la etapa de compras (la última del orden maestro→transacción) falla, el
    rollback deshace TAMBIÉN lo que ya se había flusheado antes en la misma
    corrida (clientes + ventas) — todo corre en la misma transacción."""

    async def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(importer, "_resolve_purchase_identity", _boom)

    summary: dict[str, Any] = {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "mapping_contexts": [
            {
                "context_id": "sheet:Clientes",
                "entity_type": "customer",
                "source_kind": "sheet",
                "headers": ["nombre", "documento"],
                "fields": None,
                "preview_rows": [],
                "row_count": 1,
            },
            {
                "context_id": "sheet:Ventas",
                "entity_type": "sale",
                "source_kind": "sheet",
                "headers": ["fecha", "valor"],
                "fields": None,
                "preview_rows": [],
                "row_count": 1,
            },
            {
                "context_id": "sheet:Gastos",
                "entity_type": "expense",
                "source_kind": "sheet",
                "headers": ["fecha", "monto", "producto", "cantidad"],
                "fields": None,
                "preview_rows": [],
                "row_count": 1,
            },
        ],
        "clientes_detectados": [
            {"nombre": "Juan Perez", "documento": _VALID_DNI, "__context__": "sheet:Clientes"}
        ],
        "ventas_detectadas": [
            {"fecha": "2024-01-15", "valor": "5000", "__context__": "sheet:Ventas"}
        ],
        "gastos_detectados": [
            {
                "fecha": "2024-01-15",
                "monto": "1000",
                "producto": "Yerba",
                "cantidad": "5",
                "__context__": "sheet:Gastos",
            }
        ],
        "stock_detectado": [],
    }

    with pytest.raises(RuntimeError, match="boom"):
        await importer.insert_confirmed_data(
            db_session,
            sample_tenant.tenant_id,
            summary,
            {"ventas": True, "gastos": True, "clientes": True},
            context_mappings={
                "sheet:Clientes": {"nombre": "name", "documento": "dni"},
                "sheet:Ventas": {"valor": "amount", "fecha": "transaction_date"},
                "sheet:Gastos": {"monto": "amount", "fecha": "transaction_date"},
            },
            context_confirmed={
                "sheet:Clientes": True,
                "sheet:Ventas": True,
                "sheet:Gastos": True,
            },
        )
    await db_session.rollback()

    assert (await db_session.execute(select(Customer))).first() is None
    assert (await db_session.execute(select(SaleEntry))).first() is None
    assert (await db_session.execute(select(ExpenseEntry))).first() is None
