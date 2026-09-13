"""Cambio 3 (docs/plans/conservacion-y-acceso-datos-negocio.md): exportación
completa del lado servidor — `csv_export_service.stream_entity_csv`.

Usa `isolated_db_engine` (no el `db_session` del suite estándar): esta
función abre su PROPIA sesión con `async_session_factory` — ver el docstring
del módulo sobre por qué — así que necesita un engine sobre el que pueda
comitear de verdad, mismo criterio que el resto de los tests de concurrencia
del programa de ingesta (`test_product_external_code.py`).
"""

from __future__ import annotations

import csv
import io
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import app.application.services.csv_export_service as export_service
from app.domain.verticals import Vertical
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.tests.conftest import add_business_profile

pytestmark = pytest.mark.asyncio


async def _armar_tenant(factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    tenant_id = uuid.uuid4()
    async with factory() as session:
        session.add(
            Tenant(
                tenant_id=tenant_id,
                legal_name="Export SA",
                display_name="Export SA",
                currency="ARS",
                pricing_reference_mode="MEP",
                status="ACTIVE",
            )
        )
        await session.flush()
        await add_business_profile(session, tenant_id, Vertical.KIOSCO_ALMACEN)
        await session.commit()
    return tenant_id


async def _volcar_csv(gen: AsyncIterator[str]) -> str:
    partes = [chunk async for chunk in gen]
    # El BOM UTF-8 inicial es para que Excel es-AR lea acentos — cualquier
    # lector real (csv.reader con encoding="utf-8-sig", o el propio Excel) lo
    # descarta de forma transparente antes de parsear.
    return "".join(partes).removeprefix("﻿")


@pytest.fixture
def factory(isolated_db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        isolated_db_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )


@pytest.fixture(autouse=True)
def _usar_engine_aislado(
    monkeypatch: pytest.MonkeyPatch, factory: async_sessionmaker[AsyncSession]
) -> None:
    """`stream_entity_csv` llama a `async_session_factory()` puertas adentro —
    acá apunta al engine aislado de este test, no al real."""
    monkeypatch.setattr(export_service, "async_session_factory", factory)


async def test_valores_exactos_y_formula_safe(factory: async_sessionmaker[AsyncSession]) -> None:
    tenant_id = await _armar_tenant(factory)
    async with factory() as session:
        session.add(
            Product(
                tenant_id=tenant_id,
                name="=cmd|'/c calc'!A1",  # intento de inyección de fórmula
                sku="007",  # código con ceros a la izquierda
                sale_price_ars=Decimal("15000.01"),
                unit_cost_ars=None,
                stock_units=3,
                custom_fields={"purchase_base_cost": "8500.0"},
            )
        )
        await session.commit()

    csv_text = await _volcar_csv(
        export_service.stream_entity_csv(tenant_id, Vertical.KIOSCO_ALMACEN, "product")
    )
    rows = list(csv.reader(io.StringIO(csv_text)))
    header, data = rows[0], rows[1]
    by_col = dict(zip(header, data, strict=True))

    # Fórmula neutralizada con apóstrofe inicial, valor original conservado atrás.
    assert by_col["Nombre"] == "'=cmd|'/c calc'!A1"
    # Ceros a la izquierda preservados — nunca se interpreta como número.
    assert by_col["Código (SKU)"] == "007"
    # Decimal exacto — el "$15.000" que muestra la UI redondea al entero, acá
    # tiene que sobrevivir el centavo.
    assert by_col["Precio de venta"] == "15000.01"
    # Ausencia real = cadena vacía, no "None" ni "0".
    assert by_col["Costo unitario"] == ""
    # Evidencia (F-H6.d, catálogo estático — no depende de ninguna definición
    # registrada) también viaja, aunque esté oculta en la tabla por default.
    assert by_col["Precio de compra (costo base, sin envío)"] == "8500.0"


async def test_todas_las_filas_paginando_sin_duplicar_ni_saltear(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`_PAGE_SIZE` bajado a 3 para no crear 500+ filas en un test — la lógica
    de "seguir pidiendo páginas hasta que una venga incompleta" es la misma."""
    export_service._PAGE_SIZE = 3
    tenant_id = await _armar_tenant(factory)
    nombres = [f"Producto {i}" for i in range(10)]
    async with factory() as session:
        for nombre in nombres:
            session.add(
                Product(
                    tenant_id=tenant_id,
                    name=nombre,
                    sale_price_ars=Decimal("100"),
                    stock_units=1,
                )
            )
        await session.commit()

    csv_text = await _volcar_csv(
        export_service.stream_entity_csv(tenant_id, Vertical.KIOSCO_ALMACEN, "product")
    )
    rows = list(csv.reader(io.StringIO(csv_text)))
    header = rows[0]
    nombre_idx = header.index("Nombre")
    exportados = [r[nombre_idx] for r in rows[1:]]

    assert len(exportados) == len(nombres)
    assert sorted(exportados) == sorted(nombres)  # sin duplicados ni faltantes


async def test_aislamiento_entre_tenants(factory: async_sessionmaker[AsyncSession]) -> None:
    tenant_a = await _armar_tenant(factory)
    tenant_b = await _armar_tenant(factory)
    async with factory() as session:
        session.add(
            Product(
                tenant_id=tenant_a,
                name="Solo de A",
                sale_price_ars=Decimal("1"),
                stock_units=1,
            )
        )
        session.add(
            Product(
                tenant_id=tenant_b,
                name="Solo de B",
                sale_price_ars=Decimal("1"),
                stock_units=1,
            )
        )
        await session.commit()

    csv_text = await _volcar_csv(
        export_service.stream_entity_csv(tenant_a, Vertical.KIOSCO_ALMACEN, "product")
    )
    assert "Solo de A" in csv_text
    assert "Solo de B" not in csv_text


async def test_entidad_no_soportada_lanza_value_error(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _armar_tenant(factory)
    with pytest.raises(ValueError, match="no soportada"):
        await _volcar_csv(
            export_service.stream_entity_csv(tenant_id, Vertical.KIOSCO_ALMACEN, "marketing")
        )


async def test_include_inactive_suma_los_dados_de_baja(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _armar_tenant(factory)
    async with factory() as session:
        session.add(
            Product(
                tenant_id=tenant_id,
                name="Producto inactivo",
                sale_price_ars=Decimal("1"),
                stock_units=0,
                is_active=False,
            )
        )
        await session.commit()

    solo_activos = await _volcar_csv(
        export_service.stream_entity_csv(tenant_id, Vertical.KIOSCO_ALMACEN, "product")
    )
    assert "Producto inactivo" not in solo_activos

    con_inactivos = await _volcar_csv(
        export_service.stream_entity_csv(
            tenant_id, Vertical.KIOSCO_ALMACEN, "product", include_inactive=True
        )
    )
    assert "Producto inactivo" in con_inactivos
