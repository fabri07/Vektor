"""T3 (E-2, E-3): DataRepair devuelve el stock de los duplicados y deja el chat en revisión.

E-2: anular un duplicado de venta repone su descuento (``sale:{id}``); anular un
duplicado de gasto de compra anula su ``purchase`` (baja el stock); las dos
pasan su identidad de operación a la fila conservada (la de ``VOID_SALE`` se
suelta) y guardan el ``movement_id`` en el ítem. Si anular la compra deja stock
negativo (ya se vendió) o la compra no tiene par claro, el grupo se saltea entero.

E-3: la corrección automática de ventas del chat está apagada: la venta queda
«para revisar» y no se modifica.
"""

from __future__ import annotations

import unittest.mock
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import stock_service
from app.application.services.data_repair_service import apply_repair
from app.domain.verticals import Vertical
from app.persistence.models.business import BusinessProfile
from app.persistence.models.file import UploadedFile
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.operation_identity import (
    OperationIdentity,
    OperationIdentityLink,
)
from app.persistence.models.product import Product
from app.persistence.models.repair import DataRepairItem
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.tests.integration.conftest import make_tenant, make_user

_FECHA = datetime(2026, 6, 1, 12)


@pytest.fixture(autouse=True)
def _sin_eventos() -> Any:
    with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
        yield


@pytest_asyncio.fixture
async def tenant(session: AsyncSession) -> Any:
    t = await make_tenant(session, legal_name="Kiosco T3")
    session.add(
        BusinessProfile(tenant_id=t.tenant_id, vertical_code=Vertical.KIOSCO_ALMACEN.value)
    )
    await session.commit()
    return t


async def _libro(
    session: AsyncSession, tid: uuid.UUID, uploaded_by: uuid.UUID, minuto: int
) -> UploadedFile:
    """El mismo libro (``content_hash`` igual) subido en el minuto indicado."""
    f = UploadedFile(
        id=uuid.uuid4(),
        tenant_id=tid,
        uploaded_by=uploaded_by,
        original_filename="libro.xlsx",
        s3_key=f"test/libro-{minuto}.xlsx",
        content_type="application/vnd.ms-excel",
        size_bytes=1024,
        purpose="ingestion",
        status="uploaded",
        processing_status="DONE",
        parsed_summary_json={"file_type": "spreadsheet"},
        content_hash="h-libro",
    )
    f.created_at = datetime(2026, 6, 2, 10, minuto, tzinfo=UTC)
    session.add(f)
    await session.commit()
    return f


@pytest_asyncio.fixture
async def archivo(session: AsyncSession, tenant: Any) -> UploadedFile:
    user = await make_user(session, tenant.tenant_id)
    return await _libro(session, tenant.tenant_id, user.user_id, 0)


@pytest_asyncio.fixture
async def resubido(session: AsyncSession, archivo: UploadedFile) -> UploadedFile:
    """E-1: un duplicado es la copia que entró por re-subir el MISMO archivo."""
    return await _libro(session, archivo.tenant_id, archivo.uploaded_by, 5)


async def _producto(db: AsyncSession, tid: uuid.UUID, nombre: str, stock: int) -> Product:
    p = Product(
        tenant_id=tid, name=nombre, sale_price_ars=Decimal("500"), stock_units=stock
    )
    db.add(p)
    await db.commit()
    return p


async def _identidad(db: AsyncSession, tid: uuid.UUID, tipo: str, eid: uuid.UUID) -> uuid.UUID:
    ident = OperationIdentity(
        tenant_id=tid,
        identity_key=f"k:{uuid.uuid4()}",
        content_hash=f"h:{eid}",
        entity_type=tipo,
    )
    db.add(ident)
    await db.flush()
    db.add(OperationIdentityLink(
        identity_id=ident.id, tenant_id=tid, entity_type=tipo, entity_id=eid
    ))
    await db.commit()
    return ident.id


async def _venta_importada(
    db: AsyncSession, tid: uuid.UUID, upload_id: uuid.UUID, producto: Product, fila: str
) -> SaleEntry:
    v = SaleEntry(
        tenant_id=tid, amount=Decimal("1500.00"), quantity=3, product_id=producto.id,
        transaction_date=_FECHA, payment_method="cash", notes="Coca",
        provenance="REAL", source_upload_id=upload_id, source_row_ref=fila,
    )
    v.created_at = datetime(2026, 6, 2, 12, int(fila.split(":")[1]), tzinfo=UTC)
    db.add(v)
    await db.flush()
    await stock_service.decrement_for_sale(v, db)
    await db.commit()
    return v


async def _compra_importada(
    db: AsyncSession,
    tid: uuid.UUID,
    upload_id: uuid.UUID | None,
    producto: Product,
    fila: str,
    *,
    con_fila: bool = True,
    descripcion: str = "Compra yerba",
    categoria: str = "INVENTORY",
    tipo: str = "COGS",
    qty: int = 10,
    costo: str = "100",
    custom_fields: dict[str, Any] | None = None,
) -> ExpenseEntry:
    """Gasto de compra + su ``purchase``. ``fila`` ordena la carga aunque
    ``con_fila=False`` (import viejo, sin ``source_row_ref``)."""
    ref = fila if con_fila else None
    g = ExpenseEntry(
        tenant_id=tid, amount=Decimal("1000.00"), category=categoria, expense_type=tipo,
        transaction_date=_FECHA, description=descripcion, payment_method="transfer",
        provenance="REAL", source_upload_id=upload_id, source_row_ref=ref,
        product_id=producto.id,
    )
    if custom_fields is not None:
        g.custom_fields = custom_fields
    # Orden de carga explícito: el detector se queda con el más viejo. El monto
    # va con la escala de la columna: la clave del detector compara `str(amount)`
    # y una instancia viva del identity map conserva el Decimal sin escala.
    g.created_at = datetime(2026, 6, 2, 12, int(fila.split(":")[1]), tzinfo=UTC)
    db.add(g)
    await db.flush()
    await stock_service.increment_stock(
        producto.id, tid, qty, Decimal(costo), None, db,
        source_upload_id=upload_id, source_row_ref=ref,
    )
    await db.commit()
    return g


async def _vinculos(db: AsyncSession, identity_id: uuid.UUID) -> list[uuid.UUID]:
    return list(
        (
            await db.execute(
                select(OperationIdentityLink.entity_id).where(
                    OperationIdentityLink.identity_id == identity_id
                )
            )
        ).scalars()
    )


async def _stock(db: AsyncSession, pid: uuid.UUID) -> int:
    return int(
        (await db.execute(select(Product.stock_units).where(Product.id == pid))).scalar_one()
    )


async def _vivos(db: AsyncSession, pid: uuid.UUID, tipo: str) -> list[InventoryMovement]:
    return list(
        (
            await db.execute(
                select(InventoryMovement).where(
                    InventoryMovement.product_id == pid,
                    InventoryMovement.movement_type == tipo,
                    InventoryMovement.voided_at.is_(None),
                )
            )
        ).scalars()
    )


async def _items(db: AsyncSession, run_id: uuid.UUID, accion: str) -> list[DataRepairItem]:
    return list(
        (
            await db.execute(
                select(DataRepairItem).where(
                    DataRepairItem.run_id == run_id, DataRepairItem.action == accion
                )
            )
        ).scalars()
    )


# ── E-2: duplicado de venta ──────────────────────────────────────────────────


async def test_duplicado_de_venta_repone_stock_y_pasa_su_identidad_a_la_conservada(
    session: AsyncSession, tenant: Any, archivo: UploadedFile, resubido: UploadedFile
) -> None:
    tid = tenant.tenant_id
    coca = await _producto(session, tid, "Coca", 10)
    v1 = await _venta_importada(session, tid, archivo.id, coca, "fila:2")
    v2 = await _venta_importada(session, tid, resubido.id, coca, "fila:3")
    id1 = await _identidad(session, tid, "sale", v1.id)
    id2 = await _identidad(session, tid, "sale", v2.id)
    # Una identidad compartida (el mismo comprobante): la conservada ya la tiene.
    compartida = await _identidad(session, tid, "sale", v1.id)
    session.add(OperationIdentityLink(
        identity_id=compartida, tenant_id=tid, entity_type="sale", entity_id=v2.id
    ))
    await session.commit()
    assert await _stock(session, coca.id) == 4
    mov_dup = await stock_service._live_sale_movement(v2.id, tid, session)
    assert mov_dup is not None

    r = await apply_repair(session, tenant_id=tid, dry_run=False)
    await session.commit()

    assert r.sales_voided == 1
    await session.refresh(v1)
    await session.refresh(v2)
    assert v1.voided_at is None
    assert v2.void_reason == "DUPLICATE"
    # Las 3 unidades del duplicado vuelven; la venta que queda sigue descontada.
    assert await _stock(session, coca.id) == 7
    vivos = await _vivos(session, coca.id, "sale")
    assert [m.source_event_id for m in vivos] == [f"sale:{v1.id}"]
    # La operación sigue viva en la conservada: la identidad del duplicado pasa a
    # ella (no se suelta: un re-import la volvería a crear), y la compartida
    # queda con un solo vínculo.
    assert await _vinculos(session, id2) == [v1.id]
    assert await _vinculos(session, id1) == [v1.id]
    assert await _vinculos(session, compartida) == [v1.id]
    (item,) = await _items(session, r.run_id, "VOID_DUPLICATE")
    assert item.sale_entry_id == v2.id
    assert item.after_json is not None
    assert item.after_json["movement_id"] == str(mov_dup.id)
    assert item.after_json["movement_qty"] == -3


# ── E-2: duplicado de gasto de compra ────────────────────────────────────────


async def test_duplicado_de_compra_baja_el_stock(
    session: AsyncSession, tenant: Any, archivo: UploadedFile, resubido: UploadedFile
) -> None:
    tid = tenant.tenant_id
    yerba = await _producto(session, tid, "Yerba", 0)
    g1 = await _compra_importada(session, tid, archivo.id, yerba, "fila:2")
    g2 = await _compra_importada(session, tid, resubido.id, yerba, "fila:3")
    id2 = await _identidad(session, tid, "expense", g2.id)
    assert await _stock(session, yerba.id) == 20
    (mov_g2,) = [
        m for m in await _vivos(session, yerba.id, "purchase") if m.source_row_ref == "fila:3"
    ]

    r = await apply_repair(session, tenant_id=tid, dry_run=False)
    await session.commit()

    assert r.expenses_voided == 1
    await session.refresh(g1)
    await session.refresh(g2)
    assert g1.voided_at is None
    assert g2.void_reason == "DUPLICATE"
    assert await _stock(session, yerba.id) == 10
    assert [m.source_row_ref for m in await _vivos(session, yerba.id, "purchase")] == ["fila:2"]
    assert await _vinculos(session, id2) == [g1.id]
    (item,) = await _items(session, r.run_id, "VOID_DUPLICATE")
    assert item.after_json is not None
    assert item.after_json["movement_id"] == str(mov_g2.id)
    assert item.after_json["movement_qty"] == 10


async def test_duplicado_de_compra_ya_vendida_se_saltea_sin_cambios(
    session: AsyncSession, tenant: Any, archivo: UploadedFile, resubido: UploadedFile
) -> None:
    tid = tenant.tenant_id
    yerba = await _producto(session, tid, "Yerba", 0)
    g1 = await _compra_importada(session, tid, archivo.id, yerba, "fila:2")
    g2 = await _compra_importada(session, tid, resubido.id, yerba, "fila:3")
    id2 = await _identidad(session, tid, "expense", g2.id)
    # Se vendieron 15 de las 20: anular una compra de 10 dejaría −5.
    venta = SaleEntry(
        tenant_id=tid, amount=Decimal("7500"), quantity=15, product_id=yerba.id,
        transaction_date=_FECHA, payment_method="cash", provenance="REAL",
    )
    session.add(venta)
    await session.flush()
    await stock_service.decrement_for_sale(venta, session)
    await session.commit()
    assert await _stock(session, yerba.id) == 5

    dry = await apply_repair(session, tenant_id=tid, dry_run=True)
    assert await _items(session, dry.run_id, "VOID_DUPLICATE") == []
    r = await apply_repair(session, tenant_id=tid, dry_run=False)
    await session.commit()

    assert r.expenses_voided == 0
    await session.refresh(g1)
    await session.refresh(g2)
    assert g1.voided_at is None and g2.voided_at is None
    assert await _stock(session, yerba.id) == 5
    assert len(await _vivos(session, yerba.id, "purchase")) == 2
    assert await session.get(OperationIdentity, id2) is not None
    assert await _items(session, r.run_id, "VOID_DUPLICATE") == []
    (salteado,) = [e for e in r.tenant_results if e.get("status") == "SKIPPED"]
    assert salteado["reason"] == "compra_ya_vendida"


async def test_con_source_run_id_se_aplican_los_gastos_planificados(
    session: AsyncSession, tenant: Any, archivo: UploadedFile, resubido: UploadedFile
) -> None:
    tid = tenant.tenant_id
    yerba = await _producto(session, tid, "Yerba", 0)
    await _compra_importada(session, tid, archivo.id, yerba, "fila:2")
    g2 = await _compra_importada(session, tid, resubido.id, yerba, "fila:3")
    dry = await apply_repair(session, tenant_id=tid, dry_run=True)
    await session.commit()
    # Un duplicado que aparece DESPUÉS del dry-run (otra re-subida) no está en el plan.
    tercera = await _libro(session, tid, archivo.uploaded_by, 9)
    g3 = await _compra_importada(session, tid, tercera.id, yerba, "fila:4")
    assert await _stock(session, yerba.id) == 30

    r = await apply_repair(session, tenant_id=tid, dry_run=False, source_run_id=dry.run_id)
    await session.commit()

    assert r.expenses_voided == 1
    await session.refresh(g2)
    await session.refresh(g3)
    assert g2.void_reason == "DUPLICATE"
    assert g3.voided_at is None
    assert await _stock(session, yerba.id) == 20


# ── E-3: venta del chat ──────────────────────────────────────────────────────


@pytest.mark.parametrize("dry_run", [True, False])
async def test_venta_del_chat_queda_en_revision_sin_modificarse(
    session: AsyncSession, tenant: Any, dry_run: bool
) -> None:
    tid = tenant.tenant_id
    alfajor = await _producto(session, tid, "Alfajor", 10)
    venta = SaleEntry(
        tenant_id=tid, amount=Decimal("500"), quantity=1, product_id=alfajor.id,
        transaction_date=_FECHA, payment_method="cash", notes="vendí 3 alfajor",
        provenance="REAL",
    )
    session.add(venta)
    await session.flush()
    await stock_service.decrement_for_sale(venta, session)
    await session.commit()

    r = await apply_repair(session, tenant_id=tid, dry_run=dry_run)
    await session.commit()

    await session.refresh(venta)
    assert venta.amount == Decimal("500")
    assert venta.quantity == 1
    assert venta.product_id == alfajor.id
    assert await _stock(session, alfajor.id) == 9
    assert await _items(session, r.run_id, "UPDATE_SALE") == []
    (item,) = await _items(session, r.run_id, "REVIEW_SALE")
    assert item.sale_entry_id == venta.id
    assert item.after_json is not None
    assert item.after_json["suggested_correction"]["quantity"] == 3
    assert item.after_json["suggested_correction"]["amount"] == "1500.00"
    (entrada,) = [e for e in r.tenant_results if e.get("sale_id") == str(venta.id)]
    assert entrada["status"] == "REVIEW_REQUIRED"


# ── E-2: la venta de un catálogo mal clasificado (VOID_SALE) ─────────────────


async def test_void_sale_de_catalogo_mal_clasificado_devuelve_su_descuento(
    session: AsyncSession, tenant: Any
) -> None:
    """La anulación por catálogo mal leído también devuelve stock e identidad.

    El detector toma ventas sin producto, que hoy no descuentan; si una tuvo su
    ``sale:{id}`` (por ejemplo, perdió el producto después), anularla lo devuelve.
    """
    tid = tenant.tenant_id
    user = await make_user(session, tid)
    ahora = datetime.now(UTC)
    f = UploadedFile(
        id=uuid.uuid4(), tenant_id=tid, uploaded_by=user.user_id,
        original_filename="productos.csv", s3_key="test/productos.csv",
        content_type="text/csv", size_bytes=1024, purpose="chat", status="uploaded",
        processing_status="DONE",
        parsed_summary_json={
            "has_fecha": False, "has_venta": False, "has_gasto": False,
            "has_producto": True, "columns": ["nombre", "precio"],
            "inferred_type": "ventas",
            "stock_detectado": [{"nombre": "Agua 1.5L", "precio": "300"}],
            "ventas_detectadas": [{"monto": "300"}],
            "preview_rows": [{"nombre": "Agua 1.5L", "precio": "300"}],
            "file_type": "spreadsheet",
        },
    )
    f.created_at = ahora
    session.add(f)
    otro = await _producto(session, tid, "Soda", 10)
    venta = SaleEntry(
        tenant_id=tid, amount=Decimal("300.00"), quantity=1,
        transaction_date=ahora.date(), payment_method="cash",
        notes="Importado desde archivo", provenance="REAL",
    )
    venta.created_at = ahora
    session.add(venta)
    await session.flush()
    mov = await stock_service.decrement_stock(
        otro.id, tid, 1, stock_service.sale_source_event_id(venta.id), session
    )
    await session.commit()
    ident = await _identidad(session, tid, "sale", venta.id)
    assert await _stock(session, otro.id) == 9

    r = await apply_repair(session, tenant_id=tid, dry_run=False)
    await session.commit()

    assert r.sales_voided == 1
    await session.refresh(venta)
    assert venta.void_reason == "REPAIR_MISCLASSIFIED_IMPORT"
    assert await _stock(session, otro.id) == 10
    assert await _vivos(session, otro.id, "sale") == []
    assert await session.get(OperationIdentity, ident) is None
    (item,) = await _items(session, r.run_id, "VOID_SALE")
    assert item.after_json is not None
    assert item.after_json["movement_id"] == str(mov.id)


async def test_fila_con_dos_gastos_y_una_compra_se_saltea_sin_cambios(
    session: AsyncSession, tenant: Any, archivo: UploadedFile, resubido: UploadedFile
) -> None:
    """Dos gastos de la MISMA fila de la re-subida y una sola compra: no se sabe
    de cuál es."""
    tid = tenant.tenant_id
    yerba = await _producto(session, tid, "Yerba", 0)
    g1 = await _compra_importada(session, tid, archivo.id, yerba, "fila:2")
    copia = await _compra_importada(session, tid, resubido.id, yerba, "fila:2")
    g2 = ExpenseEntry(
        tenant_id=tid, amount=Decimal("1000.00"), category="INVENTORY", expense_type="COGS",
        transaction_date=_FECHA, description="Compra yerba", payment_method="transfer",
        provenance="REAL", source_upload_id=resubido.id, source_row_ref="fila:2",
        product_id=yerba.id,
    )
    g2.created_at = datetime(2026, 6, 3, tzinfo=UTC)
    session.add(g2)
    await session.commit()
    assert await _stock(session, yerba.id) == 20

    r = await apply_repair(session, tenant_id=tid, dry_run=False)
    await session.commit()

    await session.refresh(g1)
    await session.refresh(copia)
    await session.refresh(g2)
    assert g1.voided_at is None and copia.voided_at is None and g2.voided_at is None
    assert await _stock(session, yerba.id) == 20
    assert len(await _vivos(session, yerba.id, "purchase")) == 2
    (salteado,) = [e for e in r.tenant_results if e.get("status") == "SKIPPED"]
    assert salteado["reason"] == "compra_sin_par_claro"



# ── Revisión adversarial de T3 ───────────────────────────────────────────────


@pytest.mark.parametrize("caso", ["import_viejo_sin_fila", "compra_del_chat_sin_fila"])
async def test_linea_de_compra_sin_par_no_se_anula_suelta(
    session: AsyncSession, tenant: Any, archivo: UploadedFile, resubido: UploadedFile, caso: str
) -> None:
    """Una línea de compra sin su ``purchase`` identificable: el grupo se saltea.

    Antes el plan daba «sin movimiento» y el gasto se anulaba dejando la compra
    viva (stock inflado). Import viejo: sin ``source_row_ref`` (existe desde
    20260727_0001), con la categoría vieja «mercaderia». Compra con
    ``purchase_group_id`` (la marca del chat) y sin fila. Una compra a mano o del
    chat SIN archivo ya no entra a ningún grupo (E-1: no es copia de una carga).
    """
    tid = tenant.tenant_id
    yerba = await _producto(session, tid, "Yerba", 0)
    if caso == "import_viejo_sin_fila":
        g1 = await _compra_importada(
            session, tid, archivo.id, yerba, "fila:2", con_fila=False,
            categoria="mercaderia", tipo="OPEX",
        )
        g2 = await _compra_importada(
            session, tid, resubido.id, yerba, "fila:3", con_fila=False,
            categoria="mercaderia", tipo="OPEX",
        )
    else:
        g1 = await _compra_importada(
            session, tid, archivo.id, yerba, "fila:2", descripcion="Yerba — Proveedor"
        )
        g2 = await _compra_importada(
            session, tid, resubido.id, yerba, "fila:3", con_fila=False,
            descripcion="Yerba — Proveedor",
            custom_fields={"purchase_group_id": str(uuid.uuid4())},
        )
    assert await _stock(session, yerba.id) == 20

    dry = await apply_repair(session, tenant_id=tid, dry_run=True)
    assert await _items(session, dry.run_id, "VOID_DUPLICATE") == []
    r = await apply_repair(session, tenant_id=tid, dry_run=False)
    await session.commit()

    await session.refresh(g1)
    await session.refresh(g2)
    assert g1.voided_at is None and g2.voided_at is None
    assert await _stock(session, yerba.id) == 20
    assert len(await _vivos(session, yerba.id, "purchase")) == 2
    (salteado,) = [e for e in r.tenant_results if e.get("status") == "SKIPPED"]
    assert salteado["reason"] == "compra_sin_par_claro"


async def test_dos_compras_distintas_en_la_fila_que_el_monto_no_distingue_se_saltea(
    session: AsyncSession, tenant: Any, archivo: UploadedFile, resubido: UploadedFile
) -> None:
    """Dos gastos de 1.000 y dos compras en la misma fila de la re-subida: 10 × 100
    y 5 × 200.

    El monto encaja con las dos y son distintas: no se elige por id.
    """
    tid = tenant.tenant_id
    yerba = await _producto(session, tid, "Yerba", 0)
    g0 = await _compra_importada(session, tid, archivo.id, yerba, "fila:2")
    g1 = await _compra_importada(session, tid, resubido.id, yerba, "fila:2")
    g2 = await _compra_importada(
        session, tid, resubido.id, yerba, "fila:2", qty=5, costo="200"
    )
    g2.created_at = datetime(2026, 6, 3, tzinfo=UTC)
    await session.commit()
    assert await _stock(session, yerba.id) == 25

    r = await apply_repair(session, tenant_id=tid, dry_run=False)
    await session.commit()

    await session.refresh(g0)
    await session.refresh(g1)
    await session.refresh(g2)
    assert g0.voided_at is None and g1.voided_at is None and g2.voided_at is None
    assert await _stock(session, yerba.id) == 25
    (salteado,) = [e for e in r.tenant_results if e.get("status") == "SKIPPED"]
    assert salteado["reason"] == "compra_sin_par_claro"


async def test_dry_run_acumula_el_efecto_de_grupos_anteriores_sobre_el_producto(
    session: AsyncSession, tenant: Any, archivo: UploadedFile, resubido: UploadedFile
) -> None:
    """Dos grupos de compras duplicadas del mismo producto y stock para uno solo.

    40 comprados, 25 vendidos: queda 15. El primer grupo baja 10 (queda 5); el
    segundo dejaría −5. El preview tiene que saltear lo mismo que el apply.
    """
    tid = tenant.tenant_id
    yerba = await _producto(session, tid, "Yerba", 0)
    await _compra_importada(session, tid, archivo.id, yerba, "fila:2")
    await _compra_importada(session, tid, resubido.id, yerba, "fila:3")
    await _compra_importada(session, tid, archivo.id, yerba, "fila:4", descripcion="Otra")
    await _compra_importada(session, tid, resubido.id, yerba, "fila:5", descripcion="Otra")
    venta = SaleEntry(
        tenant_id=tid, amount=Decimal("12500"), quantity=25, product_id=yerba.id,
        transaction_date=_FECHA, payment_method="cash", provenance="REAL",
    )
    session.add(venta)
    await session.flush()
    await stock_service.decrement_for_sale(venta, session)
    await session.commit()
    assert await _stock(session, yerba.id) == 15

    dry = await apply_repair(session, tenant_id=tid, dry_run=True)
    planificados = await _items(session, dry.run_id, "VOID_DUPLICATE")
    r = await apply_repair(session, tenant_id=tid, dry_run=False)
    await session.commit()

    assert len(planificados) == 1
    assert r.expenses_voided == 1
    assert await _stock(session, yerba.id) == 5
    assert [e["reason"] for e in dry.tenant_results if e.get("status") == "SKIPPED"] == [
        "compra_ya_vendida"
    ]
