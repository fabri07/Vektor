"""Cambio 1: el `value_path` de cada descriptor tiene que resolver al valor
REAL de una fila persistida — no solo enumerar nombres de campo.

`get_available_fields` (business_field_catalog_service.py) devuelve
METADATA (dónde vive cada dato), no los valores en sí — eso es trabajo de
Cambio 2. Pero el contrato que promete ("value_path resuelve a esto") tiene
que probarse ahora, con datos reales, para las 5 entidades: es exactamente
lo que pidió la revisión del primer commit ("demostrar que el dato guardado
se puede leer correctamente" en vez de solo el nombre del campo).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.business_field_catalog_service import get_available_fields
from app.domain.verticals import Vertical
from app.persistence.models.customer import Customer
from app.persistence.models.product import Product
from app.persistence.models.supplier import Supplier
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry

pytestmark = pytest.mark.asyncio


def _leer(value_path: str, row: object) -> Any:
    """Mismo resolutor que usará Cambio 2: atributo del ORM, o clave de
    `custom_fields` si el path empieza con ese prefijo."""
    if value_path.startswith("custom_fields."):
        key = value_path.removeprefix("custom_fields.")
        return (getattr(row, "custom_fields", None) or {}).get(key)
    return getattr(row, value_path)


async def test_valores_reales_de_producto(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    producto = Product(
        tenant_id=tid,
        name="Silla de living",
        sale_price_ars=Decimal("15000"),
        unit_cost_ars=Decimal("9000"),
        stock_units=3,
        custom_fields={"purchase_base_cost": "8500.0", "marca": "El pasillo"},
    )
    db_session.add(producto)
    await db_session.commit()

    campos = await get_available_fields(db_session, tid, Vertical.KIOSCO_ALMACEN, "product")
    por_key = {c["field_key"]: c for c in campos}

    assert _leer(por_key["name"]["value_path"], producto) == "Silla de living"
    assert _leer(por_key["unit_cost_ars"]["value_path"], producto) == Decimal("9000")
    assert _leer(por_key["purchase_base_cost"]["value_path"], producto) == "8500.0"
    # Un campo sin valor en esta fila resuelve a None, no explota.
    assert _leer(por_key["list_price_ars"]["value_path"], producto) is None


async def test_valores_reales_de_venta(db_session: AsyncSession, sample_tenant: Tenant) -> None:
    tid = sample_tenant.tenant_id
    venta = SaleEntry(
        tenant_id=tid,
        amount=Decimal("5000"),
        quantity=2,
        unit_price=Decimal("2500"),
        transaction_date=datetime(2026, 9, 1),
        payment_method="cash",
        notes="Venta de prueba",
    )
    db_session.add(venta)
    await db_session.commit()

    campos = await get_available_fields(db_session, tid, Vertical.KIOSCO_ALMACEN, "sale")
    por_key = {c["field_key"]: c for c in campos}

    assert _leer(por_key["amount"]["value_path"], venta) == Decimal("5000")
    assert _leer(por_key["quantity"]["value_path"], venta) == 2
    assert _leer(por_key["unit_price"]["value_path"], venta) == Decimal("2500")
    assert _leer(por_key["payment_method"]["value_path"], venta) == "cash"


async def test_valores_reales_de_gasto_incluyendo_evidencia(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    gasto = ExpenseEntry(
        tenant_id=tid,
        amount=Decimal("1200"),
        category="LOGISTICS",
        expense_type="OPEX",
        transaction_date=datetime(2026, 9, 1),
        description="Flete del remito 123",
        payment_method="transfer",
        custom_fields={"attributed_to_inventory": True},
    )
    db_session.add(gasto)
    await db_session.commit()

    campos = await get_available_fields(db_session, tid, Vertical.KIOSCO_ALMACEN, "expense")
    por_key = {c["field_key"]: c for c in campos}

    assert _leer(por_key["amount"]["value_path"], gasto) == Decimal("1200")
    assert _leer(por_key["expense_type"]["value_path"], gasto) == "OPEX"
    assert _leer(por_key["description"]["value_path"], gasto) == "Flete del remito 123"
    # Evidencia F-H6.d: vive en custom_fields, se lee igual que cualquier otro.
    assert _leer(por_key["attributed_to_inventory"]["value_path"], gasto) is True
    assert por_key["attributed_to_inventory"]["editable"] is False


async def test_valores_reales_de_cliente(db_session: AsyncSession, sample_tenant: Tenant) -> None:
    tid = sample_tenant.tenant_id
    cliente = Customer(
        tenant_id=tid,
        name="Juana",
        last_name="Pérez",
        dni="30111222",
        email="juana@example.com",
        credit_limit=Decimal("50000"),
        external_code="C-001",
    )
    db_session.add(cliente)
    await db_session.commit()

    campos = await get_available_fields(db_session, tid, Vertical.KIOSCO_ALMACEN, "customer")
    por_key = {c["field_key"]: c for c in campos}

    assert _leer(por_key["name"]["value_path"], cliente) == "Juana"
    assert _leer(por_key["dni"]["value_path"], cliente) == "30111222"
    assert _leer(por_key["credit_limit"]["value_path"], cliente) == Decimal("50000")
    assert _leer(por_key["external_code"]["value_path"], cliente) == "C-001"
    assert por_key["external_code"]["editable"] is False


async def test_valores_reales_de_proveedor(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    proveedor = Supplier(
        tenant_id=tid,
        name="Distribuidora Sur",
        cuit="30712345678",
        payment_method="transfer",
        catalog_url="https://ejemplo.com/catalogo",
    )
    db_session.add(proveedor)
    await db_session.commit()

    campos = await get_available_fields(db_session, tid, Vertical.KIOSCO_ALMACEN, "supplier")
    por_key = {c["field_key"]: c for c in campos}

    assert _leer(por_key["name"]["value_path"], proveedor) == "Distribuidora Sur"
    assert _leer(por_key["cuit"]["value_path"], proveedor) == "30712345678"
    assert _leer(por_key["catalog_url"]["value_path"], proveedor) == "https://ejemplo.com/catalogo"


async def test_un_id_al_azar_no_rompe_ningun_value_path(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Compuerta barata contra un typo de value_path en cualquiera de las 5
    entidades: crea una fila vacía de cada una y resuelve TODOS los
    descriptores — ninguno puede tirar AttributeError."""
    tid = sample_tenant.tenant_id
    filas: dict[str, object] = {
        "product": Product(
            tenant_id=tid, name="X", sale_price_ars=Decimal("1"), stock_units=0
        ),
        "sale": SaleEntry(
            tenant_id=tid,
            amount=Decimal("1"),
            quantity=1,
            transaction_date=datetime(2026, 1, 1),
        ),
        "expense": ExpenseEntry(
            tenant_id=tid,
            amount=Decimal("1"),
            category="OTHER",
            transaction_date=datetime(2026, 1, 1),
            description="x",
        ),
        "customer": Customer(tenant_id=tid, name="X"),
        "supplier": Supplier(tenant_id=tid, name="X"),
    }
    for row in filas.values():
        db_session.add(row)
    await db_session.commit()

    for entity_type, row in filas.items():
        campos = await get_available_fields(db_session, tid, Vertical.KIOSCO_ALMACEN, entity_type)
        for campo in campos:
            _leer(campo["value_path"], row)  # no debe lanzar
