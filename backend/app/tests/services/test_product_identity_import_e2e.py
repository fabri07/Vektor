"""Import end-to-end contra los índices únicos de F5-B (F5-A, A6).

Los tests de ``test_product_identity.py`` prueban el helper aislado y los de
``test_product_identity_wiring.py`` cada camino de escritura por separado. Acá se
corre el import COMPLETO (``insert_confirmed_data``) con los tres uniques activos,
que es donde se ve si el cableado miente en los ``counts`` que después alimentan el
banner de warnings del confirm (PR #13).

Cómo se simula la carrera
-------------------------
El motor de identidad decide contra índices en memoria que se cargan UNA vez al
abrir la corrida (``_load_product_identity_indexes``). La colisión que F5-A absorbe
ocurre justamente cuando ese índice quedó viejo: otra transacción ocupó la clave
después de la precarga. Se reproduce vaciando el índice precargado — el producto SÍ
está en la DB, el motor no lo ve, decide ``create``, y el índice único lo rechaza.
Eso es exactamente lo que ve una corrida que perdió la carrera, no un atajo.

Sin los uniques creados no habría colisión que disparar y estos tests pasarían
vacíos. Cuando F5-B declare los índices en el ORM, el fixture se borra y siguen
valiendo igual.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

import app.application.services.ingestion_import_service as importer
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry
from app.persistence.models.unclassified_record import UnclassifiedRecord

_DDL = (
    "CREATE UNIQUE INDEX uq_products_tenant_barcode_norm ON products "
    "(tenant_id, barcode_normalized) WHERE is_active "
    "AND barcode_normalized IS NOT NULL AND barcode_normalized <> ''",
    "CREATE UNIQUE INDEX uq_products_tenant_sku_norm ON products "
    "(tenant_id, sku_normalized) WHERE is_active "
    "AND sku_normalized IS NOT NULL AND sku_normalized <> ''",
    "CREATE UNIQUE INDEX uq_inventory_balances_tenant_product ON inventory_balances "
    "(tenant_id, product_id)",
)
_INDEX_NAMES = (
    "uq_products_tenant_barcode_norm",
    "uq_products_tenant_sku_norm",
    "uq_inventory_balances_tenant_product",
)


@pytest_asyncio.fixture
async def f5_indexes(db_session: AsyncSession) -> AsyncGenerator[None, None]:
    """Los tres uniques de F5-B, dentro de la transacción del test."""
    for stmt in _DDL:
        await db_session.execute(text(stmt))
    await db_session.flush()
    yield
    for name in _INDEX_NAMES:
        await db_session.execute(text(f"DROP INDEX IF EXISTS {name}"))


@pytest.fixture
def stale_identity_index() -> Generator[None, None, None]:
    """Índices en memoria vacíos: el motor no ve lo que ya está en la DB.

    Hay que vaciar los DOS loaders. El path de compras linkea primero con
    ``_load_product_index`` (el índice legacy sku/nombre/token) y solo si ese
    NO resuelve llega a ``_resolve_purchase_identity``. Staleando uno solo, el
    otro resuelve por SKU y el test nunca toca el código de F5-A: pasaría en
    verde sin probar nada. En una carrera real ambos se cargan en el mismo
    instante, así que quedan viejos juntos — vaciarlos juntos es lo fiel.
    """
    with (
        patch.object(
            importer,
            "_load_product_identity_indexes",
            new=AsyncMock(return_value=importer.ProductIdentityIndexes({}, {}, {}, {}, {})),
        ),
        patch.object(
            importer, "_load_product_index", new=AsyncMock(return_value=({}, {}, {}))
        ),
    ):
        yield


async def _seed_product(
    db_session: AsyncSession, tenant_id: uuid.UUID, **kw: Any
) -> Product:
    base: dict[str, Any] = {
        "tenant_id": tenant_id,
        "name": "Coca Cola 500ml",
        "sale_price_ars": Decimal("1500"),
        "stock_units": 10,
    }
    base.update(kw)
    product = Product(**base)
    db_session.add(product)
    await db_session.flush()
    return product


def _compras_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "file_type": "spreadsheet",
        "inferred_type": "gastos",
        "has_gasto": True,
        "gastos_detectados": rows,
    }


def _compra_row(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "fecha": "2024-02-01",
        "categoria": "mercaderia",
        "producto": "Coca Cola 500ml",
        "sku": "COCA-500",
        "cantidad": "24",
        "monto": "19200",
        "costo_unitario": "800",
        "forma_pago": "efectivo",
    }
    base.update(kw)
    return base


# ── SKU repetido: el import no duplica y no explota ──────────────────────────


@pytest.mark.usefixtures("f5_indexes", "stale_identity_index")
async def test_import_de_compras_con_sku_ocupado_vincula_sin_duplicar(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """El unique rechaza el INSERT; el import reusa al ocupante y sigue.

    Sin F5-A esto sería un 500 con la transacción del import abortada.
    """
    existing = await _seed_product(
        db_session, sample_tenant.tenant_id, sku="COCA-500", stock_units=10
    )

    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        _compras_summary([_compra_row()]),
        {"gastos": True},
    )

    assert counts["gastos"] == 1
    assert counts["otros"] == 0

    products = (
        (
            await db_session.execute(
                select(Product).where(Product.tenant_id == sample_tenant.tenant_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(products) == 1, "el unique tiene que haber impedido el duplicado"
    assert products[0].id == existing.id

    expense = (await db_session.execute(select(ExpenseEntry))).scalar_one()
    assert expense.product_id == existing.id, "el gasto queda vinculado al ocupante"


@pytest.mark.usefixtures("stale_identity_index")
async def test_dos_filas_con_el_mismo_sku_nuevo_reusan_por_cache(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Dos filas con el mismo SKU nuevo crean UN producto — sin índice único.

    Corre a propósito **sin** ``f5_indexes``: con el índice puesto la 2ª fila
    colisionaría contra el unique y volvería ``"linked"``, así que las aserciones
    pasarían aunque el reuso intra-corrida estuviera muerto. Sin índice, el único
    que puede evitar el 2º producto es el reuso intra-corrida.

    Alcance real, medido por mutación: el reuso lo dan DOS mecanismos redundantes
    —la caché de identidad (``_register_product_identity_cache``) y el índice
    transaccional legacy (``_register_product_transaction_indexes``)—. Desactivar
    cualquiera de los dos por separado NO regresa (el otro cubre); recién con los
    dos muertos aparecen 2 productos y ``sin_producto`` salta a 2. O sea: este test
    protege el COMPORTAMIENTO, no cada mecanismo por separado. Una regresión que
    rompa uno solo pasaría desapercibida acá, y hoy no hay nada más que la cubra.
    """
    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        _compras_summary([_compra_row(), _compra_row(monto="9600", cantidad="12")]),
        {"gastos": True},
    )

    assert counts["gastos"] == 2
    products = (
        (
            await db_session.execute(
                select(Product).where(Product.tenant_id == sample_tenant.tenant_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(products) == 1
    # Un solo producto creado, aunque haya dos filas de compra.
    assert counts["sin_producto"] == 1


# ── created vs linked: los counts no pueden mentir ───────────────────────────


@pytest.mark.usefixtures("f5_indexes", "stale_identity_index")
async def test_reuso_por_indice_no_se_reporta_como_producto_creado(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """``sin_producto`` alimenta el warning "creé un producto incompleto".

    Si el índice único resolvió la carrera reusando un producto que YA existía,
    no se creó nada: contarlo mentiría en el banner del confirm.
    """
    await _seed_product(db_session, sample_tenant.tenant_id, sku="COCA-500")

    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        _compras_summary([_compra_row()]),
        {"gastos": True},
    )

    assert counts["sin_producto"] == 0, "reuso no es creación"


@pytest.mark.usefixtures("f5_indexes", "stale_identity_index")
async def test_creacion_genuina_si_se_reporta_como_producto_creado(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Contracara del anterior: sin ocupante, la creación sí se cuenta.

    Sin este caso el test de arriba pasaría también con un contador roto en 0.
    """
    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        _compras_summary([_compra_row()]),
        {"gastos": True},
    )

    assert counts["sin_producto"] == 1
    products = (
        (
            await db_session.execute(
                select(Product).where(Product.tenant_id == sample_tenant.tenant_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(products) == 1
    assert products[0].requires_completion is True


# ── conflicto real detectado por la DB → "Otros" ─────────────────────────────


@pytest.mark.usefixtures("f5_indexes", "stale_identity_index")
async def test_conflicto_barcode_vs_sku_detectado_por_la_db_va_a_otros(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Barcode y SKU ocupados por productos DISTINTOS: nunca adivinar.

    El motor no lo ve (índice viejo) y decide crear; la DB rechaza y el helper
    descubre que las dos claves tienen dueños distintos → la fila va a "Otros"
    con AMBOS candidatos, y el gasto NO se registra.
    """
    dueno_barcode = await _seed_product(
        db_session,
        sample_tenant.tenant_id,
        name="Coca Cola retornable",
        barcode="7790895000997",
    )
    dueno_sku = await _seed_product(
        db_session, sample_tenant.tenant_id, name="Coca Cola lata", sku="COCA-500"
    )

    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        _compras_summary([_compra_row(barcode="7790895000997")]),
        {"gastos": True},
    )

    assert counts["otros"] == 1
    assert counts["gastos"] == 0, "una fila ambigua no registra el gasto"

    # Ningún 3er producto: la ambigüedad no crea nada.
    products = (
        (
            await db_session.execute(
                select(Product).where(Product.tenant_id == sample_tenant.tenant_id)
            )
        )
        .scalars()
        .all()
    )
    assert {p.id for p in products} == {dueno_barcode.id, dueno_sku.id}

    record = (await db_session.execute(select(UnclassifiedRecord))).scalar_one()
    candidate_ids = {c["id"] for c in (record.match_candidates or [])}
    assert candidate_ids == {str(dueno_barcode.id), str(dueno_sku.id)}, (
        "quien revise la fila tiene que ver los DOS productos en disputa"
    )
