"""B·3 (paso 3 del cierre del #48) — un producto nacido de una compra recibe categoría.

``build_incomplete_product`` dejaba ``category=None`` siempre: medido en producción,
0 de 398 productos activos de un cliente de decoración tenían categoría, todos
creados desde líneas de compra, y el filtro de /products no devolvía nada.

Ahora infiere desde el nombre con ``infer_category`` y asigna SÓLO con confianza
``high`` (el mismo umbral que el import de catálogo). Ambigua o sin evidencia: sin
categoría, que se ve y se corrige; una equivocada no se ve.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, patch

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

import app.application.services.ingestion_import_service as importer
from app.application.services.ingestion_import_service import build_incomplete_product
from app.domain.product_category_inference import CategorySuggestion
from app.domain.verticals import Vertical
from app.persistence.models.business import BusinessProfile
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant


async def _set_vertical(session: AsyncSession, tenant_id: uuid.UUID, vertical: Vertical) -> None:
    await session.execute(
        update(BusinessProfile)
        .where(BusinessProfile.tenant_id == tenant_id)
        .values(vertical_code=vertical.value)
    )
    await session.flush()


async def _category_of(session: AsyncSession, product_id: uuid.UUID | None) -> str | None:
    assert product_id is not None
    product = await session.get(Product, product_id)
    assert product is not None
    return product.category


# ── build_incomplete_product (helper) ─────────────────────────────────────────


async def test_inferencia_alta_asigna_la_categoria(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    await _set_vertical(db_session, tid, Vertical.DECORACION_HOGAR)

    pid, created = await build_incomplete_product(
        db_session, tid, "Lampara de pie nordica", None, Decimal("100")
    )

    assert created is True
    assert await _category_of(db_session, pid) == "ILUMINACION"
    product = await db_session.get(Product, pid)
    assert product is not None
    # La categoría no completa el producto: sigue faltando el precio de venta.
    assert product.requires_completion is True
    assert product.sale_price_ars == Decimal("0")


async def test_nombre_ambiguo_no_asigna(db_session: AsyncSession, sample_tenant: Tenant) -> None:
    """"Mesa de luz": MUEBLES por "mesa" e ILUMINACION por "luz". Dos lecturas
    posibles → no se elige por el negocio."""
    tid = sample_tenant.tenant_id
    await _set_vertical(db_session, tid, Vertical.DECORACION_HOGAR)

    pid, _ = await build_incomplete_product(db_session, tid, "Mesa de luz", None, None)

    assert await _category_of(db_session, pid) is None


async def test_confianza_media_no_asigna(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Hoy el helper no pasa especificaciones, así que ``infer_category`` no
    devuelve ``medium`` por sí sola; el día que las pase, una sugerencia media
    tiene que seguir sin aplicarse. Se fuerza para fijar el umbral."""
    tid = sample_tenant.tenant_id
    await _set_vertical(db_session, tid, Vertical.DECORACION_HOGAR)
    media = CategorySuggestion(
        code="DECO", confidence="medium", matched_text="x", rule="specifications:x"
    )
    with patch.object(importer, "infer_category", return_value=media):
        pid, _ = await build_incomplete_product(db_session, tid, "Cosa", None, None)

    assert await _category_of(db_session, pid) is None


async def test_sin_evidencia_no_asigna(db_session: AsyncSession, sample_tenant: Tenant) -> None:
    tid = sample_tenant.tenant_id
    await _set_vertical(db_session, tid, Vertical.DECORACION_HOGAR)

    pid, _ = await build_incomplete_product(db_session, tid, "Articulo 4471", None, None)

    assert await _category_of(db_session, pid) is None


async def test_rubro_sin_catalogo_no_asigna(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """El kiosco no tiene vocabulario de inferencia: nunca se usa el de otro rubro."""
    pid, _ = await build_incomplete_product(
        db_session, sample_tenant.tenant_id, "Lampara de pie", None, None
    )

    assert await _category_of(db_session, pid) is None


async def test_vertical_explicito_gana_y_no_consulta_el_perfil(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """El import pasa el rubro que ya cargó: el helper no hace una query por producto."""
    with patch.object(
        importer,
        "_try_load_tenant_vertical",
        new=AsyncMock(side_effect=AssertionError("no debía consultar el perfil")),
    ):
        pid, _ = await build_incomplete_product(
            db_session,
            sample_tenant.tenant_id,
            "Florero de vidrio",
            None,
            None,
            vertical=Vertical.DECORACION_HOGAR,
        )

    assert await _category_of(db_session, pid) == "DECO"


async def test_tenant_sin_perfil_crea_sin_categoria(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Una reclasificación de gasto (``pending_action_service``) no se rompe por un
    tenant sin rubro: el producto se crea, sin inferencia."""
    tid = sample_tenant.tenant_id
    await db_session.execute(delete(BusinessProfile).where(BusinessProfile.tenant_id == tid))
    await db_session.flush()

    pid, created = await build_incomplete_product(db_session, tid, "Lampara", None, None)

    assert created is True
    assert await _category_of(db_session, pid) is None


async def test_reuso_no_toca_la_categoria_del_existente(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Si el SKU ya es de otro producto, se reusa al ocupante tal cual: la
    inferencia nunca pisa una categoría (ni la ausencia de una) que alguien eligió."""
    tid = sample_tenant.tenant_id
    await _set_vertical(db_session, tid, Vertical.DECORACION_HOGAR)
    editado = Product(
        tenant_id=tid,
        name="Lampara colgante",
        sku="LAMP-1",
        sale_price_ars=Decimal("500"),
        stock_units=0,
        category="DECO",  # el usuario la puso en DECO a propósito
    )
    sin_categoria = Product(
        tenant_id=tid,
        name="Velador",
        sku="VEL-1",
        sale_price_ars=Decimal("500"),
        stock_units=0,
        category=None,
    )
    db_session.add_all([editado, sin_categoria])
    await db_session.flush()

    pid_a, created_a = await build_incomplete_product(
        db_session, tid, "Lampara colgante", "LAMP-1", None
    )
    pid_b, created_b = await build_incomplete_product(db_session, tid, "Velador", "VEL-1", None)

    assert (pid_a, created_a) == (editado.id, False)
    assert (pid_b, created_b) == (sin_categoria.id, False)
    await db_session.refresh(editado)
    await db_session.refresh(sin_categoria)
    assert editado.category == "DECO"
    assert sin_categoria.category is None


# ── Import de compras (camino real) ───────────────────────────────────────────


def _compra(producto: str, sku: str) -> dict[str, Any]:
    return {
        "fecha": "2024-02-01",
        "categoria": "mercaderia",
        "producto": producto,
        "sku": sku,
        "cantidad": "2",
        "monto": "2000",
        "costo_unitario": "1000",
        "forma_pago": "efectivo",
    }


async def test_import_de_compras_categoriza_solo_lo_inequivoco(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    await _set_vertical(db_session, tid, Vertical.DECORACION_HOGAR)

    # El import carga el rubro una vez y lo pasa: si el helper lo volviera a
    # buscar por producto, este mock lo delata.
    with patch.object(
        importer,
        "_try_load_tenant_vertical",
        new=AsyncMock(side_effect=AssertionError("el import debía pasar el rubro")),
    ):
        await importer.insert_confirmed_data(
            db_session,
            tid,
            {
                "file_type": "spreadsheet",
                "inferred_type": "gastos",
                "has_gasto": True,
                "gastos_detectados": [
                    # Nombres sin tokens en común: el import linkea por token, y
                    # dos filas parecidas terminarían en el mismo producto.
                    _compra("Lampara de pie nordica", "L-1"),
                    _compra("Mesa de luz roble", "M-1"),
                    _compra("Articulo 4471", "A-1"),
                ],
            },
            {"gastos": True},
        )

    products = {
        p.name: p
        for p in (
            await db_session.execute(select(Product).where(Product.tenant_id == tid))
        ).scalars()
    }
    assert set(products) == {"Lampara de pie nordica", "Mesa de luz roble", "Articulo 4471"}
    assert products["Lampara de pie nordica"].category == "ILUMINACION"
    assert products["Lampara de pie nordica"].requires_completion is True
    # "mesa" + "luz" → dos categorías posibles: sin categoría.
    assert products["Mesa de luz roble"].category is None
    # Sin evidencia: sin categoría.
    assert products["Articulo 4471"].category is None
