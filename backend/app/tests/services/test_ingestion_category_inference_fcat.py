"""F-CAT — un producto creado desde una compra deja de nacer sin categoría.

`build_incomplete_product` seteaba `category=None` con un comentario que explicaba
que la categoría de PRODUCTO no es el código de gasto de la línea. Es cierto, y
por eso no se copia el código de gasto — pero de ahí no se sigue que no haya nada
que poner: el nombre del artículo alcanza cuando hay una sola categoría posible.

Medido en producción antes de la fase: **0 de 398 productos activos con
categoría**, todos nacidos de líneas de compra. La consecuencia visible es que el
filtro de `/products` ofrecía Textiles, Iluminación y Muebles y no devolvía nada.

Este test cubre el CABLEADO (que el import le pase el vertical y persista lo
inferido); la regla de inferencia en sí vive en
`app/tests/domain/test_product_category_inference_fcat.py`.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.application.services.ingestion_import_service as importer
from app.domain.verticals import Vertical
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.tests.conftest import add_business_profile

_CTX = "compras"


@pytest.fixture(autouse=True)
def _sin_broker(mock_score_trigger: Any) -> None:
    """Sin broker, cada import paga los reintentos de kombu."""


@pytest_asyncio.fixture
async def tenant_deco(db_session: AsyncSession) -> Tenant:
    """Vertical decoración: es el de la cuenta donde se midió el problema."""
    t = Tenant(
        tenant_id=uuid.uuid4(),
        legal_name="Deco FCAT",
        display_name="Deco FCAT",
        currency="ARS",
        pricing_reference_mode="MEP",
        status="ACTIVE",
    )
    db_session.add(t)
    await db_session.flush()
    await add_business_profile(db_session, t.tenant_id, Vertical.DECORACION_HOGAR)
    await db_session.commit()
    return t


async def _importar_compras(
    session: AsyncSession, tenant: Tenant, articulos: list[str]
) -> None:
    await importer.insert_confirmed_data(
        session,
        tenant.tenant_id,
        {
            "file_type": "spreadsheet",
            "inferred_type": "mixed",
            "multi_sheet": True,
            "gastos_detectados": [
                {
                    "fecha": "2026-03-05",
                    "articulo": nombre,
                    "cantidad": "3",
                    "total": "3000",
                    "__context__": _CTX,
                }
                for nombre in articulos
            ],
            "ventas_detectadas": [],
            "stock_detectado": [],
        },
        {"gastos": True},
        context_mappings={
            _CTX: {
                "fecha": "expense_date",
                "articulo": "product_name",
                "cantidad": "quantity",
                "total": "amount",
            }
        },
        context_confirmed={_CTX: True},
    )
    await session.flush()


async def _producto(session: AsyncSession, nombre: str) -> Product:
    return (
        (await session.execute(select(Product).where(Product.name == nombre)))
        .scalars()
        .one()
    )


async def test_la_compra_crea_el_producto_con_la_categoria_inferida(
    db_session: AsyncSession, tenant_deco: Tenant
) -> None:
    await _importar_compras(db_session, tenant_deco, ["alfombra shaggy"])

    producto = await _producto(db_session, "alfombra shaggy")
    assert producto.category == "TEXTILES"
    # Sigue siendo un producto incompleto: falta el precio de venta, que una
    # compra no trae. Inferir la categoría no lo completa.
    assert producto.requires_completion is True


async def test_el_nombre_ambiguo_nace_sin_categoria(
    db_session: AsyncSession, tenant_deco: Tenant
) -> None:
    """Textil y de exterior a la vez: lo decide una persona, no el importador."""
    await _importar_compras(db_session, tenant_deco, ["alfombra felpuda exterior"])

    producto = await _producto(db_session, "alfombra felpuda exterior")
    assert producto.category is None


async def test_sin_evidencia_nace_sin_categoria_y_no_como_otros(
    db_session: AsyncSession, tenant_deco: Tenant
) -> None:
    """Nunca `OTHER`: es una categoría real y lo dejaría clasificado por nadie,
    además de sacarlo del filtro «Sin categoría» donde el usuario lo busca."""
    await _importar_compras(db_session, tenant_deco, ["agarradera tusor"])

    producto = await _producto(db_session, "agarradera tusor")
    assert producto.category is None


async def test_el_codigo_de_gasto_no_se_usa_como_categoria_de_producto(
    db_session: AsyncSession, tenant_deco: Tenant
) -> None:
    """La línea es un gasto INVENTORY y el producto NO queda en 'INVENTORY':
    son dos catálogos distintos y confundirlos fue el riesgo que el comentario
    original de `build_incomplete_product` venía marcando."""
    await _importar_compras(db_session, tenant_deco, ["agarradera tusor"])

    producto = await _producto(db_session, "agarradera tusor")
    assert producto.category != "INVENTORY"
