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
- Se lee SIN truncar y se valida la longitud real ANTES de comparar/asignar
  (`_clean_str_unbounded`): truncar primero volvería iguales a dos códigos
  distintos que comparten el prefijo de 100 caracteres, reportando un
  "conflicto" que nunca existió. Un valor que excede el límite real de la
  columna se preserva completo en `custom_fields`, nunca se asigna.
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


async def test_codigo_demasiado_largo_no_se_asigna_ni_se_trunca(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Dos códigos que comparten el prefijo de 100 caracteres pero difieren
    después NO deben terminar identificados como "el mismo código" — la
    validación de longitud pasa ANTES que la comparación de igualdad, así
    que ninguno de los dos productos con código demasiado largo llega
    siquiera a competir por el índice único."""
    tid = sample_tenant.tenant_id
    prefijo = "X" * 100
    codigo_1 = prefijo + "-UNO"
    codigo_2 = prefijo + "-DOS"
    summary = _multisheet_summary(
        [
            _multisheet_row("Producto Largo 1", codigo_1),
            _multisheet_row("Producto Largo 2", codigo_2),
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

    assert counts.get("external_code_too_long") == 2
    assert not counts.get("external_code_conflict")  # nunca llegaron a competir

    products = (
        await db_session.execute(select(Product).where(Product.tenant_id == tid))
    ).scalars().all()
    assert len(products) == 2
    for p in products:
        assert p.external_code is None  # nunca se asignó, ni truncado
        pendiente = p.custom_fields.get("_external_code_pendiente_revision")
        assert pendiente is not None
        assert len(pendiente["external_code"]) == 104  # se preservó COMPLETO
    codigos_preservados = {
        p.custom_fields["_external_code_pendiente_revision"]["external_code"] for p in products
    }
    assert codigos_preservados == {codigo_1, codigo_2}  # distintos, no colapsados


async def test_sistema_de_origen_demasiado_largo_tampoco_asigna_el_par(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Si el código entra pero el sistema de origen excede su propio límite
    (60), no se asigna NINGUNO de los dos — es "ese par" o nada."""
    tid = sample_tenant.tenant_id
    mappings = {
        "sheet:Catalogo": {
            "nombre": "name",
            "clave_z9": "external_code",
            "fuente_z9": "external_source",
            "precio": "sale_price_ars",
        },
    }
    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "has_stock": True,
        "mapping_contexts": [
            {
                "context_id": "sheet:Catalogo",
                "label": "Catalogo",
                "entity_type": "product",
                "headers": ["nombre", "clave_z9", "fuente_z9", "precio"],
                "row_count": 1,
            },
        ],
        "stock_detectado": [
            {
                "nombre": "Producto Fuente Larga",
                "clave_z9": "OK-123",
                "fuente_z9": "Y" * 61,
                "precio": "100",
                "__context__": "sheet:Catalogo",
            }
        ],
    }
    counts = await importer.insert_confirmed_data(
        db_session,
        tid,
        summary,
        {"productos": True},
        context_mappings=mappings,
        context_confirmed={"sheet:Catalogo": True},
    )

    assert counts.get("external_code_too_long") == 1
    product = (
        await db_session.execute(select(Product).where(Product.tenant_id == tid))
    ).scalar_one()
    assert product.external_code is None
    assert product.external_source is None


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


async def test_conflicto_con_return_details_no_crashea_por_atributos_expirados(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Regresión: el rollback al savepoint del conflicto deja el producto con
    los atributos expirados. Los callers (ledger de reversión F11, vía
    `product_details`) siguen leyendo el mismo objeto Python DESPUÉS del
    conflicto — sin `session.refresh()` en el except, esa lectura dispara
    `MissingGreenlet` (bug real, encontrado por review, no cubierto por los
    tests anteriores porque todos re-consultaban el producto con un SELECT
    fresco en vez de leer el objeto que el import ya tenía en mano)."""
    tid = sample_tenant.tenant_id
    summary = _multisheet_summary(
        [
            _multisheet_row("Producto G", "ERP-DUP-2"),
            _multisheet_row("Producto H", "ERP-DUP-2"),
        ]
    )
    result = await importer.insert_confirmed_data(
        db_session,
        tid,
        summary,
        {"productos": True},
        context_mappings=_MULTISHEET_MAPPINGS,
        context_confirmed={"sheet:Catalogo": True},
        return_details=True,
    )

    assert result.get("external_code_conflict") == 1
    details = result["product_details"]
    assert len(details) == 2
    assert all(d["action"] == "CREATED" for d in details)
    # Ninguna entrada debería quedar con datos a medio escribir por el
    # conflicto — ambas describen el producto tal como terminó persistido.
    for d in details:
        assert d["product_id"]
        assert Decimal(d["after"]["sale_price_ars"]) == Decimal("100")


async def test_conflicto_en_producto_existente_con_return_details_no_crashea(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Mismo caso que arriba, pero el conflicto ocurre al ENRIQUECER un
    producto YA EXISTENTE (_merge_into_existing) en vez de al crear uno
    nuevo — es el camino que arma before/after para el ledger de F11."""
    tid = sample_tenant.tenant_id
    otro = Product(
        tenant_id=tid,
        name="Producto Ocupante",
        sale_price_ars=Decimal("50"),
        external_code="ERP-OCUPADO",
    )
    db_session.add(otro)
    await db_session.flush()

    existente = Product(tenant_id=tid, name="Producto I", sale_price_ars=Decimal("100"))
    db_session.add(existente)
    await db_session.flush()

    summary = _multisheet_summary([_multisheet_row("Producto I", "ERP-OCUPADO")])
    result = await importer.insert_confirmed_data(
        db_session,
        tid,
        summary,
        {"productos": True},
        context_mappings=_MULTISHEET_MAPPINGS,
        context_confirmed={"sheet:Catalogo": True},
        return_details=True,
    )

    assert result.get("external_code_conflict") == 1
    detalle = next(d for d in result["product_details"] if d["product_id"] == str(existente.id))
    assert detalle["action"] == "UPDATED"
    assert detalle["after"]["external_code"] is None  # se descartó, no se pisó
    await db_session.refresh(existente)
    assert existente.external_code is None


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
