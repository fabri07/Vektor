"""F-S.0 mecanismo 3: `_load_product_index` tiene que resolver también por
alias, no solo por `Product.name` — si no, guardar el alias no sirve para nada
en la SIGUIENTE importación. Y la ambigüedad tiene que compararse contra el
PRODUCTO, no sólo contra "la clave ya estaba ocupada": el nombre real y un
alias del mismo producto (o dos alias del mismo producto) que normalizan
igual no pueden marcar al producto como ambiguo consigo mismo.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import ingestion_import_service as importer
from app.domain.product_alias import add_alias
from app.domain.text_norm import normalize_product_name
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant


async def test_load_product_index_resuelve_por_alias(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    product = Product(
        tenant_id=sample_tenant.tenant_id,
        name="Coca Cola 500ml",
        sale_price_ars=Decimal("1500"),
        stock_units=10,
        custom_fields=add_alias(None, "Gaseosa cola cualquiera"),
    )
    db_session.add(product)
    await db_session.flush()

    by_sku, by_name, by_token = await importer._load_product_index(
        db_session, sample_tenant.tenant_id
    )

    alias_key = normalize_product_name("Gaseosa cola cualquiera")
    assert by_name.get(alias_key) == product.id, (
        "el alias guardado tiene que resolver igual que el nombre real"
    )


async def test_alias_igual_al_nombre_con_mayuscula_y_acento_distinto_no_es_ambiguo(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Si el nombre real y un alias del MISMO producto normalizan igual (p.ej.
    el usuario vinculó una variante que ya coincidía con el nombre salvo
    mayúsculas/acentos), el producto NO puede quedar ambiguo consigo mismo."""
    product = Product(
        tenant_id=sample_tenant.tenant_id,
        name="Gaseosa Cola",
        sale_price_ars=Decimal("1500"),
        stock_units=10,
        custom_fields=add_alias(None, "gaseosa cólá"),
    )
    db_session.add(product)
    await db_session.flush()

    _, by_name, _ = await importer._load_product_index(db_session, sample_tenant.tenant_id)

    norm = normalize_product_name("Gaseosa Cola")
    assert by_name.get(norm) == product.id


async def test_dos_alias_del_mismo_producto_equivalentes_no_es_ambiguo(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Dos alias del MISMO producto que normalizan igual (agregados en
    momentos distintos, con distinta puntuación/mayúscula) tampoco lo vuelven
    ambiguo consigo mismo."""
    cf = add_alias(None, "Gaseosa Cola 500")
    cf = add_alias(cf, "gaseosa cola 500ml")  # normaliza distinto, alias #2 real
    product = Product(
        tenant_id=sample_tenant.tenant_id,
        name="Coca Cola",
        sale_price_ars=Decimal("1500"),
        stock_units=10,
        custom_fields=cf,
    )
    db_session.add(product)
    await db_session.flush()

    _, by_name, _ = await importer._load_product_index(db_session, sample_tenant.tenant_id)

    assert by_name.get(normalize_product_name("Gaseosa Cola 500")) == product.id
    assert by_name.get(normalize_product_name("gaseosa cola 500ml")) == product.id


async def test_mismo_alias_reclamado_por_dos_productos_distintos_si_es_ambiguo(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Ahora sí: el mismo alias en DOS productos distintos es una ambigüedad
    real, igual que dos productos con el mismo nombre."""
    p1 = Product(
        tenant_id=sample_tenant.tenant_id,
        name="Coca Cola 500ml",
        sale_price_ars=Decimal("1500"),
        stock_units=10,
        custom_fields=add_alias(None, "Gaseosa cola"),
    )
    p2 = Product(
        tenant_id=sample_tenant.tenant_id,
        name="Pepsi 500ml",
        sale_price_ars=Decimal("1400"),
        stock_units=5,
        custom_fields=add_alias(None, "Gaseosa cola"),
    )
    db_session.add_all([p1, p2])
    await db_session.flush()

    _, by_name, _ = await importer._load_product_index(db_session, sample_tenant.tenant_id)

    assert by_name.get(normalize_product_name("Gaseosa cola")) is None, (
        "dos productos distintos reclamando el mismo alias tiene que quedar "
        "ambiguo (None), no resolver arbitrariamente a uno de los dos"
    )
    # Los nombres reales de cada uno, sin embargo, siguen resolviendo sin
    # ambigüedad — la ambigüedad es del alias compartido, no contagia al resto.
    assert by_name.get(normalize_product_name("Coca Cola 500ml")) == p1.id
    assert by_name.get(normalize_product_name("Pepsi 500ml")) == p2.id


async def test_dato_legacy_corrupto_en_aliases_no_rompe_el_indice(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Un `custom_fields["_aliases"]` corrupto (string suelto en vez de lista)
    no puede tumbar la carga del índice — `product_aliases` ya lo tolera."""
    product = Product(
        tenant_id=sample_tenant.tenant_id,
        name="Producto Legacy",
        sale_price_ars=Decimal("1000"),
        stock_units=1,
        custom_fields={"_aliases": "dato_corrupto_no_es_lista"},
    )
    db_session.add(product)
    await db_session.flush()

    by_sku, by_name, by_token = await importer._load_product_index(
        db_session, sample_tenant.tenant_id
    )

    assert by_name.get(normalize_product_name("Producto Legacy")) == product.id
