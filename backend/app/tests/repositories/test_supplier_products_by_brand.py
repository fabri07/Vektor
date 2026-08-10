"""Tests de la jerarquía Proveedor→Marca→Producto en el detalle de proveedor.

Cubre ``products_purchased_from_supplier`` (derivación de ``brand``) + la función
pura ``group_products_by_brand`` (agrupado, orden, match oficial). El label
"Productos genéricos" NO lo pone el backend: ``brand=None`` viaja como null.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.product import Product
from app.persistence.models.supplier import Supplier
from app.persistence.models.tenant import Tenant
from app.persistence.repositories.inventory_repository import (
    InventoryRepository,
    SupplierProductPurchase,
    _norm_official,
    group_products_by_brand,
)
from app.tests.repositories._helpers import _product, _purchase


def _row(
    name: str, brand: str | None, qty: float, unit_price: str
) -> SupplierProductPurchase:
    return SupplierProductPurchase(
        product_id=uuid.uuid4(),
        name=name,
        last_purchase_at=None,
        total_qty=qty,
        unit_price=Decimal(unit_price),
        brand=brand,
    )


# ── Función pura: _norm_official ──────────────────────────────────────────────


def test_norm_official_strips_accents_case_and_spaces() -> None:
    assert _norm_official("  PLAYADÍTO ") == _norm_official("playadito")
    assert _norm_official("Coca   Cola") == "coca cola"


def test_norm_official_exact_no_contains() -> None:
    # "Distribuidora Coca Cola Norte" NO normaliza a "coca cola".
    assert _norm_official("Distribuidora Coca Cola Norte") != _norm_official("Coca Cola")


# ── Función pura: group_products_by_brand ─────────────────────────────────────


def test_group_orders_by_value_desc_null_last() -> None:
    rows = [
        _row("Fideos", None, 2, "100"),  # genérico, valor 200
        _row("Yerba", "Playadito", 10, "150"),  # valor 1500
        _row("Gaseosa", "Coca Cola", 3, "200"),  # valor 600
    ]
    groups = group_products_by_brand(rows, supplier_name="Distribuidora Norte")

    assert [g.brand for g in groups] == ["Playadito", "Coca Cola", None]
    assert groups[-1].brand is None  # genéricos siempre al final


def test_group_official_exact_match() -> None:
    rows = [_row("Yerba", " PLAYADÍTO ", 1, "100")]
    groups = group_products_by_brand(rows, supplier_name="Playadito")
    assert len(groups) == 1
    assert groups[0].is_official is True


def test_group_official_no_contains_match() -> None:
    rows = [_row("Gaseosa", "Coca Cola", 1, "100")]
    groups = group_products_by_brand(
        rows, supplier_name="Distribuidora Coca Cola Norte"
    )
    assert groups[0].is_official is False


def test_group_products_keep_repo_order_within_group() -> None:
    a = _row("Prod A", "Marca", 1, "100")
    b = _row("Prod B", "Marca", 1, "100")
    groups = group_products_by_brand([a, b], supplier_name="X")
    assert [p.name for p in groups[0].products] == ["Prod A", "Prod B"]


def test_group_merges_normalized_brand_variants() -> None:
    """Dos variantes crudas de la misma marca → UN grupo, un solo is_official.

    Label = la variante más frecuente; empate 1-1 → la primera en orden de entrada.
    """
    rows = [
        _row("Yerba 1", "Playadito", 1, "100"),
        _row("Yerba 2", " PLAYADÍTO ", 1, "100"),
    ]
    groups = group_products_by_brand(rows, supplier_name="Playadito")
    assert len(groups) == 1
    assert groups[0].brand == "Playadito"
    assert groups[0].is_official is True
    assert len(groups[0].products) == 2


def test_group_label_is_most_frequent_variant() -> None:
    """Con >1 variante, el label del grupo es la cruda que más veces aparece."""
    rows = [
        _row("A", " coca cola ", 1, "100"),
        _row("B", "Coca Cola", 1, "100"),
        _row("C", "Coca Cola", 1, "100"),
    ]
    groups = group_products_by_brand(rows, supplier_name="X")
    assert len(groups) == 1
    assert groups[0].brand == "Coca Cola"  # 2 apariciones vs 1


# ── Repo: derivación de brand desde custom_fields ─────────────────────────────


async def test_brand_none_when_marca_missing_empty_or_whitespace(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    supplier = Supplier(id=uuid.uuid4(), tenant_id=tid, name="Prov")
    db_session.add(supplier)
    await db_session.flush()
    p_missing = await _product(db_session, tid, "Sin campo marca")  # {}
    p_empty = await _product(db_session, tid, "Marca vacía", marca="")
    p_ws = await _product(db_session, tid, "Marca espacios", marca="   ")
    for p in (p_missing, p_empty, p_ws):
        await _purchase(db_session, tid, p.id, supplier.id, 1, Decimal("100"))
    await db_session.commit()

    rows = await InventoryRepository(db_session).products_purchased_from_supplier(
        tid, supplier.id
    )
    assert {r.name: r.brand for r in rows} == {
        "Sin campo marca": None,
        "Marca vacía": None,
        "Marca espacios": None,
    }


async def test_brand_from_legacy_proveedor_key(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """custom_fields legacy ``{"proveedor": ...}`` (sin ``marca``) es el fallback."""
    tid = sample_tenant.tenant_id
    supplier = Supplier(id=uuid.uuid4(), tenant_id=tid, name="Prov")
    db_session.add(supplier)
    await db_session.flush()
    p = Product(
        id=uuid.uuid4(),
        tenant_id=tid,
        name="Gaseosa",
        sale_price_ars=Decimal("100"),
        stock_units=5,
        provenance="REAL",
        custom_fields={"proveedor": "Coca Cola"},
    )
    db_session.add(p)
    await db_session.flush()
    await _purchase(db_session, tid, p.id, supplier.id, 1, Decimal("100"))
    await db_session.commit()

    rows = await InventoryRepository(db_session).products_purchased_from_supplier(
        tid, supplier.id
    )
    assert rows[0].brand == "Coca Cola"


async def test_marca_wins_over_legacy_proveedor_key(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Si están ambas claves, ``marca`` gana sobre la legacy ``proveedor``."""
    tid = sample_tenant.tenant_id
    supplier = Supplier(id=uuid.uuid4(), tenant_id=tid, name="Prov")
    db_session.add(supplier)
    await db_session.flush()
    p = Product(
        id=uuid.uuid4(),
        tenant_id=tid,
        name="Gaseosa",
        sale_price_ars=Decimal("100"),
        stock_units=5,
        provenance="REAL",
        custom_fields={"marca": "Pepsi", "proveedor": "Coca Cola"},
    )
    db_session.add(p)
    await db_session.flush()
    await _purchase(db_session, tid, p.id, supplier.id, 1, Decimal("100"))
    await db_session.commit()

    rows = await InventoryRepository(db_session).products_purchased_from_supplier(
        tid, supplier.id
    )
    assert rows[0].brand == "Pepsi"


async def test_grouped_two_brands_plus_generic(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    supplier = Supplier(id=uuid.uuid4(), tenant_id=tid, name="Distribuidora Norte")
    db_session.add(supplier)
    await db_session.flush()
    # Playadito: 10*150 = 1500 ; Coca Cola: 3*200 = 600 ; genérico: 2*100 = 200
    p_yerba = await _product(db_session, tid, "Yerba", marca="Playadito")
    p_gaseosa = await _product(db_session, tid, "Gaseosa", marca="Coca Cola")
    p_fideos = await _product(db_session, tid, "Fideos", marca=None)
    await _purchase(db_session, tid, p_yerba.id, supplier.id, 10, Decimal("150"))
    await _purchase(db_session, tid, p_gaseosa.id, supplier.id, 3, Decimal("200"))
    await _purchase(db_session, tid, p_fideos.id, supplier.id, 2, Decimal("100"))
    await db_session.commit()

    rows = await InventoryRepository(db_session).products_purchased_from_supplier(
        tid, supplier.id
    )
    groups = group_products_by_brand(rows, supplier.name)

    assert [g.brand for g in groups] == ["Playadito", "Coca Cola", None]
    assert groups[-1].brand is None
    assert all(g.is_official is False for g in groups)


async def test_official_match_only_from_this_supplier(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """La marca comprada a OTRO proveedor no habilita el match oficial acá."""
    tid = sample_tenant.tenant_id
    playadito = Supplier(id=uuid.uuid4(), tenant_id=tid, name="Playadito")
    otro = Supplier(id=uuid.uuid4(), tenant_id=tid, name="Otro Mayorista")
    db_session.add_all([playadito, otro])
    await db_session.flush()
    # Producto marca "Playadito" comprado a "Otro Mayorista", no a "Playadito".
    p = await _product(db_session, tid, "Yerba", marca="Playadito")
    await _purchase(db_session, tid, p.id, otro.id, 1, Decimal("100"))
    await db_session.commit()

    rows = await InventoryRepository(db_session).products_purchased_from_supplier(
        tid, otro.id
    )
    groups = group_products_by_brand(rows, otro.name)
    # El nombre del proveedor "Otro Mayorista" no coincide con la marca "Playadito".
    assert groups[0].is_official is False


async def test_official_match_true_with_accents(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    supplier = Supplier(id=uuid.uuid4(), tenant_id=tid, name="Playadito")
    db_session.add(supplier)
    await db_session.flush()
    p = await _product(db_session, tid, "Yerba", marca=" PLAYADÍTO ")
    await _purchase(db_session, tid, p.id, supplier.id, 1, Decimal("100"))
    await db_session.commit()

    rows = await InventoryRepository(db_session).products_purchased_from_supplier(
        tid, supplier.id
    )
    groups = group_products_by_brand(rows, supplier.name)
    assert groups[0].brand == "PLAYADÍTO"
    assert groups[0].is_official is True


async def test_sentinel_supplier_groups_normally(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """El sentinela 'No identificado' agrupa igual, sin código especial."""
    tid = sample_tenant.tenant_id
    sentinel = Supplier(
        id=uuid.uuid4(),
        tenant_id=tid,
        name="No identificado",
        custom_fields={"_sentinel": "true"},
    )
    db_session.add(sentinel)
    await db_session.flush()
    p_marca = await _product(db_session, tid, "Yerba", marca="Playadito")
    p_gen = await _product(db_session, tid, "Fideos", marca=None)
    await _purchase(db_session, tid, p_marca.id, sentinel.id, 2, Decimal("100"))
    await _purchase(db_session, tid, p_gen.id, sentinel.id, 1, Decimal("50"))
    await db_session.commit()

    rows = await InventoryRepository(db_session).products_purchased_from_supplier(
        tid, sentinel.id
    )
    groups = group_products_by_brand(rows, sentinel.name)
    assert [g.brand for g in groups] == ["Playadito", None]
    assert all(g.is_official is False for g in groups)


async def test_ultima_compra_por_fecha_de_negocio_no_por_carga(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """F6-B3: la fecha y el costo de "última compra" salen del movimiento con el
    occurred_at (fecha de negocio) más reciente, NO del created_at (fecha de carga).

    Escenario del bug: la compra cargada DESPUÉS (created_at posterior) ocurrió
    ANTES (occurred_at anterior). Con created_at puro, la UI mostraba la fecha de
    una compra y el costo de otra. Con COALESCE(occurred_at, created_at), fecha y
    costo salen del MISMO movimiento (el de negocio más reciente).
    """
    from datetime import UTC, datetime

    tid = sample_tenant.tenant_id
    supplier = Supplier(id=uuid.uuid4(), tenant_id=tid, name="Mayorista")
    db_session.add(supplier)
    await db_session.flush()
    p = await _product(db_session, tid, "Aceite", marca="Natura")

    # Compra vieja (negocio) pero cargada última: occurred_at enero, created_at marzo.
    await _purchase(
        db_session, tid, p.id, supplier.id, 5, Decimal("100"),
        occurred_at=datetime(2026, 1, 10, tzinfo=UTC),
        created_at=datetime(2026, 3, 1, tzinfo=UTC),
    )
    # Compra reciente (negocio) pero cargada primero: occurred_at febrero, created_at feb.
    await _purchase(
        db_session, tid, p.id, supplier.id, 3, Decimal("200"),
        occurred_at=datetime(2026, 2, 20, tzinfo=UTC),
        created_at=datetime(2026, 2, 20, tzinfo=UTC),
    )
    await db_session.commit()

    rows = await InventoryRepository(db_session).products_purchased_from_supplier(
        tid, supplier.id
    )
    assert len(rows) == 1
    # Última compra = la de negocio más reciente (20/02), no la cargada última (01/03).
    assert rows[0].last_purchase_at is not None
    assert rows[0].last_purchase_at.date() == date(2026, 2, 20)
    # El costo sale del MISMO movimiento (200), no del cargado último (100).
    assert rows[0].unit_price == Decimal("200")


async def test_ultima_compra_desempate_por_id_es_deterministico(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """F6-B3: dos compras con el MISMO instante de negocio → el costo elegido es
    determinístico (desempate por id.desc()), no arbitrario."""
    from datetime import UTC, datetime

    tid = sample_tenant.tenant_id
    supplier = Supplier(id=uuid.uuid4(), tenant_id=tid, name="Mayorista")
    db_session.add(supplier)
    await db_session.flush()
    p = await _product(db_session, tid, "Fideos", marca="Marca")
    same_instant = datetime(2026, 5, 1, tzinfo=UTC)
    id_low = uuid.UUID("00000000-0000-0000-0000-000000000001")
    id_high = uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
    # occurred_at Y created_at iguales: así el ÚNICO desempate posible es id.desc()
    # (sin él, la vieja `created_at.desc()` no distinguía y el resultado quedaba
    # indefinido — el test no aislaba el tiebreak).
    await _purchase(
        db_session, tid, p.id, supplier.id, 1, Decimal("100"),
        occurred_at=same_instant, created_at=same_instant, movement_id=id_low,
    )
    await _purchase(
        db_session, tid, p.id, supplier.id, 1, Decimal("200"),
        occurred_at=same_instant, created_at=same_instant, movement_id=id_high,
    )
    await db_session.commit()

    rows = await InventoryRepository(db_session).products_purchased_from_supplier(
        tid, supplier.id
    )
    # Empate de instante → gana el id mayor (200), de forma estable.
    assert rows[0].unit_price == Decimal("200")


async def test_voided_movements_excluded(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Movimientos con voided_at no cuentan (regresión — el repo ya filtra)."""
    tid = sample_tenant.tenant_id
    supplier = Supplier(id=uuid.uuid4(), tenant_id=tid, name="Mayorista")
    db_session.add(supplier)
    await db_session.flush()
    p = await _product(db_session, tid, "Harina", marca="Pureza")
    await _purchase(db_session, tid, p.id, supplier.id, 7, Decimal("50"))
    await _purchase(db_session, tid, p.id, supplier.id, 7, Decimal("50"), voided=True)
    await db_session.commit()

    rows = await InventoryRepository(db_session).products_purchased_from_supplier(
        tid, supplier.id
    )
    groups = group_products_by_brand(rows, supplier.name)
    assert len(groups) == 1
    assert len(groups[0].products) == 1
    assert groups[0].products[0].total_qty == pytest.approx(7.0)
