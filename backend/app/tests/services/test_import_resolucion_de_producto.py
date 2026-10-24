"""El import no pega productos distintos ni carga en productos dados de baja.

Revisión profunda 2026-10-24:
- I1-2: el emparejamiento por palabras salteaba las que el catálogo no conocía,
  así que «Yerba Taragui 1kg» se pegaba a «Yerba Rosamonte 1kg» (stock, costo y
  margen en el producto equivocado), también dentro de un mismo libro de
  compras. Una palabra distintiva desconocida ahora hace abstenerse; una medida
  o un número («500», «1kg») se sigue ignorando.
- I1-1: el índice del import incluía productos inactivos. Después de una fusión
  el nombre del canónico quedaba ambiguo (ventas a «Otros»), y un producto dado
  de baja recibía compras y ventas.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.application.services.ingestion_import_service as importer
from app.domain.business_time import now_ar_naive
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.tests._respaldo import archivo_de_prueba, producto_con_compra

_COMPRAS = "sheet:Compras"
_MAPEO_COMPRAS = {
    _COMPRAS: {
        "fecha": "expense_date",
        "producto": "product_name",
        "cantidad": "quantity",
        "monto": "amount",
        "categoria": "category",
    }
}


def _libro_de_compras(filas: list[tuple[str, int, str]]) -> dict[str, Any]:
    return {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "mapping_contexts": [
            {
                "context_id": _COMPRAS,
                "entity_type": "expense",
                "source_kind": "sheet",
                "headers": ["fecha", "producto", "cantidad", "monto", "categoria"],
                "fields": None,
                "preview_rows": [],
                "row_count": len(filas),
            }
        ],
        "gastos_detectados": [
            {
                "fecha": "2024-01-15",
                "producto": nombre,
                "cantidad": Decimal(cantidad),
                "monto": monto,
                "categoria": "Mercadería",
                "__context__": _COMPRAS,
            }
            for nombre, cantidad, monto in filas
        ],
        "ventas_detectadas": [],
        "stock_detectado": [],
    }


async def _importar_compras(
    db: AsyncSession, tenant: Tenant, filas: list[tuple[str, int, str]]
) -> None:
    await importer.insert_confirmed_data(
        db,
        tenant.tenant_id,
        _libro_de_compras(filas),
        {"gastos": True},
        context_mappings=_MAPEO_COMPRAS,
        context_confirmed={_COMPRAS: True},
        uploaded_file_id=await archivo_de_prueba(db, tenant.tenant_id),
    )
    await db.flush()


async def _importar_ventas(db: AsyncSession, tenant: Tenant, ventas: list[dict[str, Any]]) -> None:
    await importer.insert_confirmed_data(
        db,
        tenant.tenant_id,
        {
            "file_type": "spreadsheet",
            "inferred_type": "ventas",
            "has_venta": True,
            "ventas_detectadas": ventas,
        },
        {"ventas": True},
        uploaded_file_id=await archivo_de_prueba(db, tenant.tenant_id),
    )
    await db.flush()


async def _productos(db: AsyncSession, tenant: Tenant) -> dict[str, Product]:
    filas = (
        await db.execute(
            select(Product).where(Product.tenant_id == tenant.tenant_id, Product.is_active)
        )
    ).scalars()
    return {p.name: p for p in filas}


async def _baja(db: AsyncSession, tenant: Tenant, nombre: str, **extra: Any) -> Product:
    p = Product(
        tenant_id=tenant.tenant_id,
        name=nombre,
        sale_price_ars=Decimal("100"),
        stock_units=7,
        is_active=False,
        **extra,
    )
    db.add(p)
    await db.flush()
    return p


# ── I1-2: palabras que el catálogo no conoce ─────────────────────────────────


async def test_dos_marcas_que_comparten_palabras_quedan_dos_productos(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    await _importar_compras(
        db_session,
        sample_tenant,
        [("Yerba Rosamonte 1kg", 10, "10000"), ("Yerba Taragui 1kg", 12, "14400")],
    )
    productos = await _productos(db_session, sample_tenant)
    assert productos["Yerba Rosamonte 1kg"].stock_units == 10
    assert productos["Yerba Taragui 1kg"].stock_units == 12
    gastos = (
        await db_session.execute(
            select(ExpenseEntry).where(ExpenseEntry.tenant_id == sample_tenant.tenant_id)
        )
    ).scalars().all()
    assert {g.product_id for g in gastos} == {
        productos["Yerba Rosamonte 1kg"].id,
        productos["Yerba Taragui 1kg"].id,
    }


async def test_una_variante_no_se_pega_al_producto_base(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    base = await producto_con_compra(db_session, sample_tenant.tenant_id, nombre="Coca Cola 500")
    await _importar_compras(db_session, sample_tenant, [("Coca Cola Zero 500", 6, "6000")])

    await db_session.refresh(base)
    assert base.stock_units == 100  # el del stock inicial: la Zero no le sumó
    assert "Coca Cola Zero 500" in await _productos(db_session, sample_tenant)


async def test_una_venta_de_otra_marca_no_se_vincula(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    brahma = await producto_con_compra(
        db_session, sample_tenant.tenant_id, nombre="Cerveza Brahma"
    )
    await _importar_ventas(
        db_session,
        sample_tenant,
        [{"fecha": "2024-01-15", "monto": "1500", "producto": "Cerveza Quilmes"}],
    )
    vinculadas = (
        await db_session.execute(select(SaleEntry).where(SaleEntry.product_id == brahma.id))
    ).scalars().all()
    assert vinculadas == []
    await db_session.refresh(brahma)
    assert brahma.stock_units == 100


async def test_una_medida_distinta_sigue_vinculando(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """La regla que no cambia: «500» vs «500ml» no es otro producto."""
    coca = await producto_con_compra(db_session, sample_tenant.tenant_id, nombre="Coca-Cola 500ml")
    await _importar_ventas(
        db_session,
        sample_tenant,
        [{"fecha": "2024-01-15", "monto": "1500", "producto": "Coca Cola 500"}],
    )
    (venta,) = (
        await db_session.execute(
            select(SaleEntry).where(SaleEntry.tenant_id == sample_tenant.tenant_id)
        )
    ).scalars().all()
    assert venta.product_id == coca.id


# ── I1-1: productos inactivos ────────────────────────────────────────────────


async def test_despues_de_una_fusion_la_venta_va_al_canonico(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    canonico = await producto_con_compra(db_session, sample_tenant.tenant_id, nombre="Coca 500")
    await _baja(db_session, sample_tenant, "Coca 500")  # el duplicado fusionado

    await _importar_ventas(
        db_session,
        sample_tenant,
        [{"fecha": "2024-01-15", "monto": "1500", "producto": "Coca 500"}],
    )
    (venta,) = (
        await db_session.execute(
            select(SaleEntry).where(SaleEntry.tenant_id == sample_tenant.tenant_id)
        )
    ).scalars().all()
    assert venta.product_id == canonico.id


async def test_un_sku_compartido_con_un_inactivo_va_al_activo(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    activo = await producto_con_compra(db_session, sample_tenant.tenant_id, nombre="Alfajor")
    activo.sku = "ALF-1"
    await _baja(db_session, sample_tenant, "Alfajor viejo", sku="alf-1")
    await db_session.flush()

    await _importar_ventas(
        db_session,
        sample_tenant,
        [{"fecha": "2024-01-15", "monto": "500", "producto": "Alfajor viejo", "sku": "ALF-1"}],
    )
    (venta,) = (
        await db_session.execute(
            select(SaleEntry).where(SaleEntry.tenant_id == sample_tenant.tenant_id)
        )
    ).scalars().all()
    assert venta.product_id == activo.id


async def test_una_compra_de_un_producto_dado_de_baja_crea_uno_nuevo(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    muerto = await _baja(db_session, sample_tenant, "Galletitas Oreo")
    await _importar_compras(db_session, sample_tenant, [("Galletitas Oreo", 10, "5000")])

    await db_session.refresh(muerto)
    assert muerto.stock_units == 7  # no recibió la compra
    nuevo = (await _productos(db_session, sample_tenant))["Galletitas Oreo"]
    assert nuevo.id != muerto.id
    assert nuevo.stock_units == 10


async def test_el_vinculo_tardio_no_cuenta_los_inactivos_como_ambiguedad(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """`_vincular_ventas_pendientes` (catálogo + historial en una tabla) usa la
    misma regla: un homónimo dado de baja no vuelve ambiguo el nombre."""
    tid = sample_tenant.tenant_id
    activo = await producto_con_compra(db_session, tid, nombre="Vela aromatica")
    await _baja(db_session, sample_tenant, "Vela aromatica")
    venta = SaleEntry(
        tenant_id=tid,
        amount=Decimal("100"),
        quantity=1,
        transaction_date=now_ar_naive(),
        payment_method="cash",
        provenance="REAL",
    )
    db_session.add(venta)
    await db_session.flush()
    db_session.info[importer._B5E_VINCULO_PENDIENTE] = {venta.id: ("Vela aromatica", None, None)}

    await importer._vincular_ventas_pendientes(db_session, tid, [venta])
    assert venta.product_id == activo.id


# ── Revisión adversarial: variaciones de escritura y tamaño ──────────────────


async def test_otro_tamano_no_se_pega_al_unico_de_la_marca(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    chica = await producto_con_compra(db_session, sample_tenant.tenant_id, nombre="Coca Cola 500ml")
    await _importar_compras(db_session, sample_tenant, [("Coca Cola 2250", 6, "12000")])

    await db_session.refresh(chica)
    assert chica.stock_units == 100
    assert (await _productos(db_session, sample_tenant))["Coca Cola 2250"].stock_units == 6


async def test_un_plural_o_un_envase_no_crea_un_duplicado(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Abstenerse en una compra crea un producto: las variaciones de escritura
    de siempre (plural, puntuación, envase) tienen que seguir vinculando."""
    tid = sample_tenant.tenant_id
    alfajor = await producto_con_compra(db_session, tid, nombre="Alfajor Jorgito (negro)")
    oreo = await producto_con_compra(db_session, tid, nombre="Galletitas Oreo")
    await _importar_compras(
        db_session,
        sample_tenant,
        [("Alfajores Jorgito negro", 10, "5000"), ("Galletitas Oreo pack", 4, "3000")],
    )
    await db_session.refresh(alfajor)
    await db_session.refresh(oreo)
    assert (alfajor.stock_units, oreo.stock_units) == (110, 104)
    assert set(await _productos(db_session, sample_tenant)) == {
        "Alfajor Jorgito (negro)",
        "Galletitas Oreo",
    }


async def test_la_marca_cargada_aparte_tambien_identifica(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    yerba = await producto_con_compra(db_session, sample_tenant.tenant_id, nombre="Yerba 1kg")
    yerba.custom_fields = {"marca": "Taragüi"}  # → brand_normalized al flush
    await db_session.flush()
    await _importar_compras(db_session, sample_tenant, [("Yerba Taragüi 1kg", 5, "7500")])

    await db_session.refresh(yerba)
    assert yerba.stock_units == 105


async def test_las_ventas_del_libro_multihoja_siguen_la_misma_regla(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    rosamonte = await producto_con_compra(db_session, tid, nombre="Yerba Rosamonte 1kg")
    await importer.insert_confirmed_data(
        db_session,
        tid,
        {
            "file_type": "spreadsheet",
            "inferred_type": "mixed",
            "multi_sheet": True,
            "mapping_contexts": [
                {
                    "context_id": "sheet:Ventas",
                    "entity_type": "sale",
                    "source_kind": "sheet",
                    "headers": ["fecha", "producto", "cantidad", "monto"],
                    "fields": None,
                    "preview_rows": [],
                    "row_count": 1,
                }
            ],
            "ventas_detectadas": [
                {
                    "fecha": "2024-01-15",
                    "producto": "Yerba Taragui 1kg",
                    "cantidad": "1",
                    "monto": "3000",
                    "__context__": "sheet:Ventas",
                }
            ],
            "gastos_detectados": [],
            "stock_detectado": [],
        },
        {"ventas": True},
        context_mappings={
            "sheet:Ventas": {
                "fecha": "transaction_date",
                "producto": "product_name",
                "cantidad": "quantity",
                "monto": "amount",
            }
        },
        context_confirmed={"sheet:Ventas": True},
        uploaded_file_id=await archivo_de_prueba(db_session, tid),
    )
    await db_session.flush()
    vinculadas = (
        await db_session.execute(select(SaleEntry).where(SaleEntry.product_id == rosamonte.id))
    ).scalars().all()
    assert vinculadas == []
