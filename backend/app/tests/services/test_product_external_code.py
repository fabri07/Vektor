"""E6a-B (quirúrgico): `external_code`/`external_source` en Product.

El catálogo de mapeo ofrece "Código en tu sistema" → `external_code` para la
entidad `product` desde F10, pero ningún constructor de `Product` lo persistía
— el valor mapeado se leía del archivo y se descartaba en silencio.

Diseño verificado con el usuario antes de implementar (rechazó una primera
versión que solo cubría un camino, descartaba conflictos en silencio, no
protegía contra concurrencia real y no integraba con la reversión F11):

- Cubre las 2 rutas de catálogo: tabla suelta (`_insert_confirmed_data_impl`)
  y multi-hoja (`_add_product`/`_insert_multisheet_data`). `external_code` NO
  aplica a compras/gastos (`build_incomplete_product`): esa entidad no tiene
  el target field, no es una omisión.
- `external_code` NUNCA es clave de matching: `_resolve_product_identity`
  sigue sin conocerlo. Un choque de código entre dos productos YA
  distinguidos por barcode/sku/nombre nunca se resuelve fusionándolos (a
  diferencia de `add_product_or_reuse`) — se descarta el campo para esa fila
  vía `external_code_guard` (`product_identity.py`), se cuenta
  (`counts["external_code_conflict"]`) y el import sigue.
- Protegido a nivel de BASE DE DATOS, no solo en memoria: el lease de import
  es per-archivo y el lock de mantenimiento es shared, así que dos imports
  del mismo tenant SÍ pueden competir por el mismo código.
- Aditivo: nunca pisa un `external_code` ya cargado.
- Integrado con F11 (`PRODUCT_RESTORE_FIELDS`) — ver el caso de reversión en
  `test_file_deletion_revert.py::test_restaura_el_codigo_externo...`.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import app.application.services.ingestion_import_service as importer
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.tests.conftest import add_business_profile

pytestmark = pytest.mark.asyncio


def _multisheet_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "has_stock": True,
        "mapping_contexts": [
            {
                "context_id": "sheet:Catalogo",
                "label": "Catalogo",
                "entity_type": "product",
                "headers": ["nombre", "clave_z9", "precio"],
                "row_count": len(rows),
            },
        ],
        "stock_detectado": rows,
    }


_MULTISHEET_MAPPINGS = {
    "sheet:Catalogo": {
        "nombre": "name",
        "clave_z9": "external_code",
        "precio": "sale_price_ars",
    },
}


def _multisheet_row(nombre: str, codigo: str, precio: str = "100") -> dict[str, Any]:
    return {
        "nombre": nombre,
        "clave_z9": codigo,
        "precio": precio,
        "__context__": "sheet:Catalogo",
    }


def _tabla_suelta_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "file_type": "spreadsheet",
        "inferred_type": "stock",
        "has_producto": True,
        "stock_detectado": rows,
    }


_TABLA_SUELTA_MAPPINGS = {
    "Productos": "name",
    "Clave Z9": "external_code",
    "Precio de venta": "sale_price_ars",
}


async def test_multihoja_alta_nueva_persiste_external_code(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    summary = _multisheet_summary([_multisheet_row("Producto A", "ERP-001")])
    await importer.insert_confirmed_data(
        db_session,
        tid,
        summary,
        {"productos": True},
        context_mappings=_MULTISHEET_MAPPINGS,
        context_confirmed={"sheet:Catalogo": True},
    )

    product = (
        await db_session.execute(select(Product).where(Product.tenant_id == tid))
    ).scalar_one()
    assert product.external_code == "ERP-001"
    assert product.external_code_key is not None


async def test_tabla_suelta_alta_nueva_persiste_external_code(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    summary = _tabla_suelta_summary(
        [{"Productos": "Producto B", "Clave Z9": "ERP-002", "Precio de venta": "200"}]
    )
    await importer.insert_confirmed_data(
        db_session,
        tid,
        summary,
        {"productos": True},
        column_mappings=_TABLA_SUELTA_MAPPINGS,
    )

    product = (
        await db_session.execute(select(Product).where(Product.tenant_id == tid))
    ).scalar_one()
    assert product.external_code == "ERP-002"


async def test_reimport_del_mismo_archivo_es_idempotente(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    summary = _multisheet_summary([_multisheet_row("Producto C", "ERP-003")])

    await importer.insert_confirmed_data(
        db_session,
        tid,
        summary,
        {"productos": True},
        context_mappings=_MULTISHEET_MAPPINGS,
        context_confirmed={"sheet:Catalogo": True},
    )
    counts = await importer.insert_confirmed_data(
        db_session,
        tid,
        summary,
        {"productos": True},
        context_mappings=_MULTISHEET_MAPPINGS,
        context_confirmed={"sheet:Catalogo": True},
    )

    assert not counts.get("external_code_conflict")
    products = (
        await db_session.execute(select(Product).where(Product.tenant_id == tid))
    ).scalars().all()
    assert len(products) == 1
    assert products[0].external_code == "ERP-003"


async def test_no_pisa_un_external_code_ya_cargado(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Política aditiva: un código cargado a mano (o por un import previo) NO
    se reemplaza por el que trae una reimportación posterior."""
    tid = sample_tenant.tenant_id
    producto = Product(
        tenant_id=tid,
        name="Producto D",
        sale_price_ars=Decimal("100"),
        external_code="CODIGO-MANUAL",
        external_source="carga_manual",
    )
    db_session.add(producto)
    await db_session.flush()

    summary = _multisheet_summary([_multisheet_row("Producto D", "ERP-004")])
    await importer.insert_confirmed_data(
        db_session,
        tid,
        summary,
        {"productos": True},
        context_mappings=_MULTISHEET_MAPPINGS,
        context_confirmed={"sheet:Catalogo": True},
    )

    await db_session.refresh(producto)
    assert producto.external_code == "CODIGO-MANUAL"


async def test_dos_filas_mismo_codigo_identidad_distinta_no_fusiona(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Dos productos GENUINAMENTE distintos (nombres distintos, no matchean
    por identidad) declaran el mismo external_code en el mismo archivo: la
    primera fila lo setea, la segunda lo descarta — nunca se fusionan en un
    solo producto, a diferencia de una colisión de barcode/sku."""
    tid = sample_tenant.tenant_id
    summary = _multisheet_summary(
        [
            _multisheet_row("Producto E", "ERP-DUP"),
            _multisheet_row("Producto F", "ERP-DUP"),
        ]
    )
    counts = await importer.insert_confirmed_data(
        db_session,
        tid,
        summary,
        {"productos": True},
        context_mappings=_MULTISHEET_MAPPINGS,
        context_confirmed={"sheet:Catalogo": True},
    )

    assert counts.get("external_code_conflict") == 1
    products = (
        await db_session.execute(select(Product).where(Product.tenant_id == tid))
    ).scalars().all()
    assert len(products) == 2  # ambos productos se crearon, ninguno se fusionó
    codes = sorted(p.external_code for p in products if p.external_code)
    assert codes == ["ERP-DUP"]  # solo uno de los dos se quedó con el código


async def test_mismo_codigo_en_tenants_distintos_no_interfiere(
    db_session: AsyncSession,
) -> None:
    tenant_a = Tenant(
        tenant_id=uuid.uuid4(),
        legal_name="A",
        display_name="A",
        currency="ARS",
        pricing_reference_mode="MEP",
        status="ACTIVE",
    )
    tenant_b = Tenant(
        tenant_id=uuid.uuid4(),
        legal_name="B",
        display_name="B",
        currency="ARS",
        pricing_reference_mode="MEP",
        status="ACTIVE",
    )
    db_session.add_all([tenant_a, tenant_b])
    await db_session.flush()
    await add_business_profile(db_session, tenant_a.tenant_id)
    await add_business_profile(db_session, tenant_b.tenant_id)

    for tenant in (tenant_a, tenant_b):
        summary = _multisheet_summary([_multisheet_row("Producto Compartido", "ERP-COMPARTIDO")])
        counts = await importer.insert_confirmed_data(
            db_session,
            tenant.tenant_id,
            summary,
            {"productos": True},
            context_mappings=_MULTISHEET_MAPPINGS,
            context_confirmed={"sheet:Catalogo": True},
        )
        assert not counts.get("external_code_conflict")

    for tenant in (tenant_a, tenant_b):
        product = (
            await db_session.execute(
                select(Product).where(Product.tenant_id == tenant.tenant_id)
            )
        ).scalar_one()
        assert product.external_code == "ERP-COMPARTIDO"


async def test_concurrencia_real_entre_dos_imports_separados(
    isolated_db_engine: AsyncEngine,
) -> None:
    """El caso que un índice en memoria NO puede cubrir: dos imports SEPARADOS
    del mismo tenant (dos transacciones/sesiones distintas, cada una con su
    propia carga de identidad) compitiendo por el mismo external_code. La
    protección real es el índice único de la base, no el estado en memoria de
    ninguna de las dos corridas."""
    factory = async_sessionmaker(
        isolated_db_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    tenant_id = uuid.uuid4()
    async with factory() as setup_session:
        tenant = Tenant(
            tenant_id=tenant_id,
            legal_name="Concurrencia",
            display_name="Concurrencia",
            currency="ARS",
            pricing_reference_mode="MEP",
            status="ACTIVE",
        )
        setup_session.add(tenant)
        await setup_session.flush()
        await add_business_profile(setup_session, tenant_id)
        await setup_session.commit()

    # Import 1: crea el producto y le asigna el código — corre y COMMITEA
    # completo, simulando un import ya terminado.
    async with factory() as session_a:
        summary_a = _multisheet_summary([_multisheet_row("Producto Import 1", "ERP-RACE")])
        await importer.insert_confirmed_data(
            session_a,
            tenant_id,
            summary_a,
            {"productos": True},
            context_mappings=_MULTISHEET_MAPPINGS,
            context_confirmed={"sheet:Catalogo": True},
        )
        await session_a.commit()

    # Import 2: SESIÓN NUEVA, sin ningún estado en memoria del import 1 — su
    # propio índice de identidad se carga fresco y no ve nada raro (el
    # producto del import 1 no matchea por nombre/sku). Aun así, declarar el
    # MISMO código externo para un producto DISTINTO tiene que chocar contra
    # el índice único de la base, no colarse.
    async with factory() as session_b:
        summary_b = _multisheet_summary([_multisheet_row("Producto Import 2", "ERP-RACE")])
        counts_b = await importer.insert_confirmed_data(
            session_b,
            tenant_id,
            summary_b,
            {"productos": True},
            context_mappings=_MULTISHEET_MAPPINGS,
            context_confirmed={"sheet:Catalogo": True},
        )
        await session_b.commit()

    assert counts_b.get("external_code_conflict") == 1

    async with factory() as verify_session:
        products = (
            await verify_session.execute(
                select(Product).where(Product.tenant_id == tenant_id)
            )
        ).scalars().all()
        assert len(products) == 2
        codes = [p.external_code for p in products if p.external_code]
        assert codes == ["ERP-RACE"]  # solo el import 1 se quedó con el código
