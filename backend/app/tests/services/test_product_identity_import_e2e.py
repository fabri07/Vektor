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
from collections.abc import Generator
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.application.services.ingestion_import_service as importer
from app.domain.text_norm import normalize_product_name
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry
from app.persistence.models.unclassified_record import UnclassifiedRecord


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


@pytest.mark.usefixtures("stale_identity_index")
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


async def test_dos_filas_con_el_mismo_sku_nuevo_reusan_por_cache(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """La 2ª fila con la misma clave sale por la CACHÉ, sin volver al savepoint.

    Este test reemplaza a uno e2e que, con los uniques de F5-B puestos, pasaba
    VACÍO: el índice hacía que la 2ª fila volviera ``"linked"`` igual, así que las
    aserciones (1 producto, ``sin_producto == 1``) se cumplían aunque el reuso
    intra-corrida estuviera muerto. Y el e2e tampoco distinguía la caché del índice
    transaccional legacy, que cubre el mismo comportamiento por otra vía.

    Por eso se llama al motor DIRECTO y se afirman las dos cosas por separado:
    que el producto queda registrado en la caché bajo sus claves, y que la 2ª
    llamada resuelve ahí — sin tocar ``build_incomplete_product``, que es quien
    abre el savepoint. Un spy de conteo sobre el import completo no alcanzaría:
    el índice legacy también evita la 2ª llamada.
    """
    indexes = importer.ProductIdentityIndexes({}, {}, {}, {}, {})
    cache: dict[str, Product] = {}
    product_cache: dict[uuid.UUID, Any] = {}

    async def _resolver() -> tuple[str, uuid.UUID | None, list[dict[str, Any]]]:
        return await importer._resolve_purchase_identity(
            db_session,
            sample_tenant.tenant_id,
            name="Coca Cola 500ml",
            sku="COCA-500",
            brand=None,
            barcode=None,
            unit_cost=Decimal("800"),
            indexes=indexes,
            cache=cache,
            product_cache=product_cache,
        )

    accion, product_id, _ = await _resolver()
    assert accion == "created"
    assert product_id is not None

    # 1) El producto quedó en la caché bajo TODAS sus claves de identidad.
    creado = product_cache[product_id]
    assert cache["sku:coca-500"] is creado
    assert cache[f"name:{normalize_product_name('Coca Cola 500ml')}"] is creado

    # 2) La 2ª fila resuelve por caché: no vuelve a construir el Product ni a abrir
    #    el savepoint. Si el registro en caché se rompe, este mock no se llama y el
    #    assert de abajo falla — es lo que el test viejo no discriminaba.
    with patch.object(
        importer, "build_incomplete_product", new=AsyncMock()
    ) as no_deberia_llamarse:
        accion_2, product_id_2, _ = await _resolver()

    assert accion_2 == "linked"
    assert product_id_2 == product_id
    no_deberia_llamarse.assert_not_called()


async def test_el_producto_reusado_tambien_queda_en_la_cache(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """El reuso por índice único también puebla la caché (perf de F5-A).

    Cuando el unique rechaza el INSERT y ``build_incomplete_product`` devuelve al
    ocupante, la fila resuelve igual — pero si ese ocupante no se registra en la
    caché, CADA fila siguiente con la misma clave vuelve a construir el Product,
    abre un savepoint y se come el ``IntegrityError``: N savepoints y 2N roundtrips
    en el camino caliente del import. El comportamiento observable es idéntico en
    los dos casos, así que sin este test la optimización puede morir en silencio.
    """
    ocupante = await _seed_product(db_session, sample_tenant.tenant_id, sku="COCA-500")

    # Índice vacío = el motor no ve al ocupante y decide ``create``; el unique lo
    # rechaza y ``build_incomplete_product`` lo reusa. Es la carrera de F5-A.
    indexes = importer.ProductIdentityIndexes({}, {}, {}, {}, {})
    cache: dict[str, Product] = {}
    product_cache: dict[uuid.UUID, Any] = {}

    accion, product_id, _ = await importer._resolve_purchase_identity(
        db_session,
        sample_tenant.tenant_id,
        name="Coca Cola 500ml",
        sku="COCA-500",
        brand=None,
        barcode=None,
        unit_cost=Decimal("800"),
        indexes=indexes,
        cache=cache,
        product_cache=product_cache,
    )

    assert accion == "linked", "reusar al ocupante no es crear"
    assert product_id == ocupante.id
    assert cache["sku:coca-500"].id == ocupante.id


# ── created vs linked: los counts no pueden mentir ───────────────────────────


@pytest.mark.usefixtures("stale_identity_index")
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


@pytest.mark.usefixtures("stale_identity_index")
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


@pytest.mark.usefixtures("stale_identity_index")
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
