"""M·P2a: historial materializado del costo promedio, escrito con el ledger real.

Las compras y las salidas se cargan con los mismos escritores que usa la app
(``stock_service``), no con filas a mano: la precarga tiene que leer lo que el
ledger realmente guarda.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import costo_promedio_service as svc
from app.application.services import stock_service
from app.application.services.reparacion_stock_inicial_service import REPAIR_TYPE
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.product_cost import ProductCostHistory, ProductCostState
from app.persistence.models.repair import DataRepairItem, DataRepairRun
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.repositories import costo_promedio_repository as repo


def _dia(n: int) -> datetime:
    return datetime(2026, 3, n, 12, 0)


async def _producto(db: AsyncSession, tenant: Tenant, nombre: str = "Vela") -> Product:
    p = Product(
        tenant_id=tenant.tenant_id,
        name=nombre,
        sale_price_ars=Decimal("300"),
        stock_units=0,
    )
    db.add(p)
    await db.flush()
    return p


async def _compra(db: AsyncSession, p: Product, qty: int, costo: str | None, dia: int) -> None:
    await stock_service.increment_stock(
        p.id, p.tenant_id, qty, None if costo is None else Decimal(costo), None, db,
        occurred_at=_dia(dia),
    )


async def _venta(
    db: AsyncSession, p: Product, qty: int, dia: int, *, descuenta: bool = True
) -> SaleEntry:
    venta = SaleEntry(
        tenant_id=p.tenant_id,
        product_id=p.id,
        amount=Decimal("300") * qty,
        quantity=qty,
        transaction_date=_dia(dia),
        payment_method="cash",
    )
    db.add(venta)
    await db.flush()
    if descuenta:
        await stock_service.decrement_stock(
            p.id, p.tenant_id, qty, stock_service.sale_source_event_id(venta.id), db,
            occurred_at=_dia(dia),
        )
    return venta


def _q(v: Decimal | None) -> Decimal | None:
    return None if v is None else v.quantize(Decimal("0.01"))


def test_el_literal_de_mr_es_el_del_servicio() -> None:
    assert repo.REPAIR_TYPE_MR == REPAIR_TYPE
    assert stock_service.sale_source_event_id(uuid.uuid4()).startswith(repo.PREFIJO_VENTA)


async def test_aceptacion_con_el_ledger_real(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    p = await _producto(db_session, sample_tenant)
    await _compra(db_session, p, 10, "100", 1)
    await _venta(db_session, p, 5, 2)
    await _compra(db_session, p, 10, "200", 3)

    r = await svc.reconstruir(db_session, sample_tenant.tenant_id, medir_brecha=True)

    assert (r.productos, r.con_registros, r.registros) == (1, 1, 2)
    assert (r.aproximados, r.fuera_del_ledger) == (0, 0)
    assert r.por_estado == {"medido": 1}
    vigente = await svc.costo_a(db_session, sample_tenant.tenant_id, p.id, date(2026, 3, 3))
    assert vigente is not None
    assert _q(vigente.costo_promedio) == Decimal("166.67")
    assert (vigente.estado, vigente.clase, vigente.stock_conocido) == ("medido", "compra", True)
    antes = await svc.costo_a(db_session, sample_tenant.tenant_id, p.id, date(2026, 3, 2))
    assert antes is not None and _q(antes.costo_promedio) == Decimal("100.00")
    assert await svc.costo_a(db_session, sample_tenant.tenant_id, p.id, date(2026, 2, 28)) is None
    # Reposición = última compra ($200): el promedio queda 1/6 por debajo.
    assert [_q(b) for b in r.brechas] == [Decimal("0.17")]


async def test_venta_importada_sin_descontar_stock_es_aproximado(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    p = await _producto(db_session, sample_tenant)
    await _compra(db_session, p, 10, "100", 1)
    await _venta(db_session, p, 3, 2, descuenta=False)

    r = await svc.reconstruir(db_session, sample_tenant.tenant_id)

    assert r.aproximados == 1
    vigente = await svc.costo_a(db_session, sample_tenant.tenant_id, p.id, date(2026, 3, 2))
    assert vigente is not None and vigente.aproximado
    estado = (
        await db_session.execute(
            select(ProductCostState).where(ProductCostState.product_id == p.id)
        )
    ).scalar_one()
    assert estado.aproximado and estado.revision == 1 and estado.pendiente_desde is None


async def test_stock_fuera_del_ledger_queda_sin_medicion(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    p = await _producto(db_session, sample_tenant)
    p.stock_units = 20  # alta con stock por fuera del ledger (POST /products)
    await db_session.flush()
    await _compra(db_session, p, 10, "100", 1)

    r = await svc.reconstruir(db_session, sample_tenant.tenant_id)

    assert r.fuera_del_ledger == 1
    assert r.por_estado == {"sin_medicion": 1}
    vigente = await svc.costo_a(db_session, sample_tenant.tenant_id, p.id, date(2026, 3, 1))
    assert vigente is not None
    assert (vigente.costo_promedio, vigente.stock_conocido) == (None, False)


async def test_stock_clampado_en_cero_no_es_fuera_del_ledger(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Se vende más de lo que hay: el ledger queda negativo y el stock en 0."""
    p = await _producto(db_session, sample_tenant)
    await _compra(db_session, p, 5, "100", 1)
    await _venta(db_session, p, 8, 2)
    await db_session.refresh(p)
    assert p.stock_units == 0

    r = await svc.reconstruir(db_session, sample_tenant.tenant_id)

    assert r.fuera_del_ledger == 0


async def test_un_clamp_anterior_no_es_stock_fuera_del_ledger(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Compra 5, venta 8 (el stock se clampa en 0, el ledger queda −3) y compra
    10: el stock es 10 y el ledger 7. No hubo stock por fuera: es aproximado."""
    p = await _producto(db_session, sample_tenant)
    await _compra(db_session, p, 5, "100", 1)
    await _venta(db_session, p, 8, 2)
    await _compra(db_session, p, 10, "200", 3)
    await db_session.refresh(p)
    assert p.stock_units == 10

    r = await svc.reconstruir(db_session, sample_tenant.tenant_id)

    assert (r.fuera_del_ledger, r.aproximados) == (0, 1)
    vigente = await svc.costo_a(db_session, sample_tenant.tenant_id, p.id, date(2026, 3, 3))
    assert vigente is not None and vigente.estado == "medido" and vigente.aproximado


async def test_una_devolucion_posterior_se_lee_sin_medicion(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    p = await _producto(db_session, sample_tenant)
    await _compra(db_session, p, 10, "100", 1)
    db_session.add(
        InventoryMovement(
            tenant_id=tid, product_id=p.id, movement_type="return", qty=2, occurred_at=_dia(2)
        )
    )
    p.stock_units = 12
    await db_session.flush()

    r = await svc.reconstruir(db_session, tid)

    assert r.por_estado == {"sin_medicion": 1}
    vigente = await svc.costo_a(db_session, tid, p.id, date(2026, 3, 5))
    assert vigente is not None
    assert (vigente.estado, vigente.costo_promedio, vigente.clase) == (
        "sin_medicion", None, "complejo"
    )
    antes = await svc.costo_a(db_session, tid, p.id, date(2026, 3, 1))
    assert antes is not None and antes.estado == "medido"


async def test_venta_sin_cantidad_no_marca_aproximado(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    p = await _producto(db_session, sample_tenant)
    await _compra(db_session, p, 10, "100", 1)
    await _venta(db_session, p, 0, 2, descuenta=False)

    r = await svc.reconstruir(db_session, sample_tenant.tenant_id)

    assert r.aproximados == 0


async def test_la_brecha_no_cuenta_los_aproximados(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    p = await _producto(db_session, sample_tenant)
    await _compra(db_session, p, 10, "100", 1)
    await _compra(db_session, p, 10, "200", 2)
    await _venta(db_session, p, 3, 3, descuenta=False)

    r = await svc.reconstruir(db_session, sample_tenant.tenant_id, medir_brecha=True)

    assert r.aproximados == 1 and r.brechas == []


async def test_catalogo_reparado_por_mr_es_apertura(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    p = await _producto(db_session, sample_tenant)
    mov = InventoryMovement(
        tenant_id=tid, product_id=p.id, movement_type="purchase", qty=10,
        unit_cost=Decimal("80"), source_type="catalog_initial_stock", occurred_at=_dia(1),
    )
    db_session.add(mov)
    p.stock_units = 10
    run = DataRepairRun(tenant_id=tid, repair_type=REPAIR_TYPE, status="APPLIED", dry_run=False)
    db_session.add(run)
    await db_session.flush()
    db_session.add(
        DataRepairItem(
            run_id=run.id, tenant_id=tid, action="RECLASSIFY_EXPENSE",
            before_json={"expense_id": str(uuid.uuid4()), "movement_id": str(mov.id)},
            confidence="HIGH",
        )
    )
    await db_session.flush()

    await svc.reconstruir(db_session, tid)

    vigente = await svc.costo_a(db_session, tid, p.id, date(2026, 3, 1))
    assert vigente is not None and vigente.clase == "apertura"
    assert vigente.costo_promedio == Decimal("80")


async def test_reconstruir_es_idempotente_y_no_toca_otro_tenant(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    p = await _producto(db_session, sample_tenant)
    await _compra(db_session, p, 10, "100", 1)
    await _compra(db_session, p, 10, "200", 2)

    await svc.reconstruir(db_session, tid)
    primera = (
        await db_session.execute(
            select(ProductCostHistory.costo_promedio).order_by(ProductCostHistory.dia)
        )
    ).scalars().all()
    await svc.reconstruir(db_session, tid)
    segunda = (
        await db_session.execute(
            select(ProductCostHistory.costo_promedio).order_by(ProductCostHistory.dia)
        )
    ).scalars().all()

    assert primera == segunda and len(segunda) == 2
    estado = (
        await db_session.execute(
            select(ProductCostState).where(ProductCostState.product_id == p.id)
        )
    ).scalar_one()
    assert estado.revision == 2
    otro = await svc.reconstruir(db_session, uuid.uuid4())
    assert otro.productos == 0
    total = await db_session.scalar(select(func.count()).select_from(ProductCostHistory))
    assert total == 2


async def test_solo_los_productos_pedidos(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    a = await _producto(db_session, sample_tenant, "A")
    b = await _producto(db_session, sample_tenant, "B")
    await _compra(db_session, a, 10, "100", 1)
    await _compra(db_session, b, 10, "300", 1)

    r = await svc.reconstruir(db_session, tid, [a.id])

    assert r.productos == 1
    assert await svc.costo_a(db_session, tid, b.id, date(2026, 3, 1)) is None
