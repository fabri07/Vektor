"""Ninguna escritura de stock pisa una venta concurrente de la otra caja (T2).

Qué se protege
--------------
Revisión profunda 2026-10-24 (A-1, B-4, C-1, A-2, B-5): ``stock_units`` y
``InventoryBalance.current_qty`` se escriben como valor ABSOLUTO calculado en
memoria. Anular un ticket o una venta, editarla, registrar una merma, sumar una
compra o un ajuste leían el producto sin candado —o con un ``FOR UPDATE`` que
devolvía la instancia vieja del identity map— y escribían sobre esa lectura.
Si en el medio commiteaba una venta de la otra caja, se perdía: stock 10, la
caja vende 3, se anula un ticket de 2 → quedaba 12 en vez de 9.

Cada test reproduce la carrera con dos conexiones: la «otra caja» bloquea el
producto y descuenta 3 sin commitear; la operación bajo prueba arranca con el
producto ya cargado en su sesión (como lo dejan las rutas) y espera; la caja
commitea; el stock final y el balance tienen que contar las dos.

Por qué contra Postgres: SQLite no tiene candados de fila; la suite pasaría
sin probar nada (``[[feedback_sqlite_masks_postgres]]``). Para correrlo::

    TEST_PG_DSN='postgresql+asyncpg://vektor:vektor@localhost:5432/vektor_pgtest' \\
        pytest app/tests/integration/test_stock_candado_de_fila_t2_pg.py -m postgres -n 0 --no-cov
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import BackgroundTasks
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.api.v1 import pos as pos_router
from app.application.services import compra_service, pos_discrepancy_service, stock_service
from app.domain.comprobante_de_compra import LineaDeCompra
from app.persistence.models.inventory import InventoryBalance, InventoryMovement
from app.persistence.models.pos_operation import PosOperation, PosOperationLine
from app.persistence.models.pos_stock_discrepancy import PosStockDiscrepancy
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.models.user import User

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]

Operacion = Callable[[AsyncSession], Awaitable[object]]


@pytest_asyncio.fixture(scope="module")
async def pg_engine() -> AsyncGenerator[AsyncEngine, None]:
    """NullPool: cada sesión con su conexión física, si no no hay contención."""
    assert TEST_PG_DSN is not None
    engine = create_async_engine(TEST_PG_DSN, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(text("SELECT 1"))
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def sm(pg_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(pg_engine, expire_on_commit=False, autoflush=False)


@pytest_asyncio.fixture
async def tenant_id(sm: async_sessionmaker[AsyncSession]) -> AsyncGenerator[uuid.UUID, None]:
    tid = uuid.uuid4()
    async with sm() as s:
        s.add(Tenant(tenant_id=tid, legal_name="T2", display_name="T2"))
        await s.commit()
    try:
        yield tid
    finally:
        async with sm() as s:
            for model in (
                PosStockDiscrepancy,
                PosOperationLine,
                PosOperation,
                InventoryMovement,
                InventoryBalance,
                SaleEntry,
            ):
                await s.execute(delete(model).where(model.tenant_id == tid))
            await s.commit()


async def _producto(
    sm: async_sessionmaker[AsyncSession], tid: uuid.UUID, stock: int = 10
) -> uuid.UUID:
    pid = uuid.uuid4()
    async with sm() as s:
        s.add(
            Product(
                id=pid,
                tenant_id=tid,
                name=f"Yerba {pid.hex[:6]}",
                sale_price_ars=Decimal("100.00"),
                unit_cost_ars=Decimal("50.00"),
                stock_units=stock,
            )
        )
        await s.flush()
        s.add(InventoryBalance(tenant_id=tid, product_id=pid, current_qty=stock, reserved_qty=0))
        await s.commit()
    return pid


async def _venta_viva(
    sm: async_sessionmaker[AsyncSession], tid: uuid.UUID, pid: uuid.UUID, qty: int
) -> uuid.UUID:
    """Una venta ya descontada (movimiento `sale` vivo), sin tocar el stock:
    el stock del escenario ya es el posterior a esa venta."""
    sale_id = uuid.uuid4()
    async with sm() as s:
        s.add(
            SaleEntry(
                id=sale_id,
                tenant_id=tid,
                product_id=pid,
                amount=Decimal("100.00") * qty,
                quantity=qty,
                transaction_date=datetime.now(UTC).replace(tzinfo=None),
            )
        )
        await s.flush()
        s.add(
            InventoryMovement(
                tenant_id=tid,
                product_id=pid,
                movement_type="sale",
                qty=-qty,
                source_event_id=stock_service.sale_source_event_id(sale_id),
            )
        )
        await s.commit()
    return sale_id


async def _carrera(
    sm: async_sessionmaker[AsyncSession],
    tid: uuid.UUID,
    pids: list[uuid.UUID],
    operacion: Operacion,
    *,
    vende: uuid.UUID | None = None,
) -> object:
    """La otra caja vende 3 de ``vende`` (o del primero) y no commitea; la
    operación arranca con los productos ya cargados y espera; la caja commitea."""
    caja = sm()
    objetivo = vende or pids[0]
    await stock_service.check_stock_available(objetivo, tid, 3, caja)
    await stock_service.decrement_stock(objetivo, tid, 3, f"pos:{uuid.uuid4()}", caja)

    async def _bajo_prueba() -> object:
        async with sm() as s:
            # Como lo dejan las rutas: producto y balance cargados antes, sin
            # candado, y RETENIDOS — el identity map es débil, y una instancia
            # que nadie referencia se libera y el FOR UPDATE la relee fresca
            # aunque le falte `populate_existing`, que es justo lo que se prueba.
            retenidos: list[object] = []
            for pid in pids:
                retenidos.append(await s.get(Product, pid))
                retenidos.extend(
                    (
                        await s.execute(
                            select(InventoryBalance).where(InventoryBalance.product_id == pid)
                        )
                    ).scalars()
                )
            resultado = await operacion(s)
            assert all(r is not None for r in retenidos)
            await s.commit()
            return resultado

    tarea = asyncio.create_task(_bajo_prueba())
    await asyncio.sleep(0.4)
    assert not tarea.done(), "la operación no esperó a la venta de la otra caja"
    await caja.commit()
    await caja.close()
    return await asyncio.wait_for(tarea, timeout=10)


async def _stock(sm: async_sessionmaker[AsyncSession], pid: uuid.UUID) -> tuple[int, int]:
    async with sm() as s:
        stock = (await s.execute(select(Product.stock_units).where(Product.id == pid))).scalar_one()
        balance = (
            await s.execute(
                select(InventoryBalance.current_qty).where(InventoryBalance.product_id == pid)
            )
        ).scalar_one()
    return stock, balance


async def test_anular_una_venta_no_pisa_la_de_la_otra_caja(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    pid = await _producto(sm, tenant_id)
    sale_id = await _venta_viva(sm, tenant_id, pid, 2)

    async def anular(s: AsyncSession) -> object:
        return await stock_service.revert_sale_stock(sale_id, tenant_id, s)

    await _carrera(sm, tenant_id, [pid], anular)
    assert await _stock(sm, pid) == (9, 9)  # 10 − 3 de la caja + 2 repuestos


async def test_reactivar_un_movimiento_no_pisa_la_venta(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    pid = await _producto(sm, tenant_id)
    async with sm() as s:
        mov = InventoryMovement(
            tenant_id=tenant_id,
            product_id=pid,
            movement_type="purchase",
            qty=5,
            unit_cost=Decimal("50.00"),
            voided_at=datetime.now(UTC),
        )
        s.add(mov)
        await s.commit()
        mov_id = mov.id

    async def reactivar(s: AsyncSession) -> object:
        movimiento = await s.get(InventoryMovement, mov_id)
        assert movimiento is not None
        await stock_service.unvoid_movement(movimiento, s)
        return None

    await _carrera(sm, tenant_id, [pid], reactivar)
    assert await _stock(sm, pid) == (12, 12)


async def test_una_merma_no_pisa_la_venta(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    pid = await _producto(sm, tenant_id)

    async def merma(s: AsyncSession) -> object:
        return await stock_service.register_stock_loss(pid, tenant_id, 1, "rotura", None, s)

    await _carrera(sm, tenant_id, [pid], merma)
    assert await _stock(sm, pid) == (6, 6)


async def test_una_compra_suelta_no_pisa_la_venta(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    pid = await _producto(sm, tenant_id)

    async def compra(s: AsyncSession) -> object:
        return await stock_service.increment_stock(
            pid, tenant_id, 20, Decimal("50.00"), None, s
        )

    await _carrera(sm, tenant_id, [pid], compra)
    assert await _stock(sm, pid) == (27, 27)


async def test_la_compra_manual_no_pisa_la_venta(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    pid = await _producto(sm, tenant_id)

    async def compra(s: AsyncSession) -> object:
        return await compra_service.registrar_compra(
            s,
            tenant_id,
            None,
            lineas=[LineaDeCompra(product_id=pid, cantidad=20, importe=Decimal("1000.00"))],
            fecha=datetime.now(UTC).replace(tzinfo=None),
            medio_de_pago="cash",
        )

    await _carrera(sm, tenant_id, [pid], compra)
    assert await _stock(sm, pid) == (27, 27)


async def test_un_ajuste_de_conciliacion_no_pisa_la_venta(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    pid = await _producto(sm, tenant_id)

    async def ajuste(s: AsyncSession) -> object:
        return await stock_service.register_reconciliation_adjustment(
            pid, tenant_id, 4, "estaban", f"rec:{uuid.uuid4()}", s
        )

    await _carrera(sm, tenant_id, [pid], ajuste)
    assert await _stock(sm, pid) == (11, 11)


async def test_una_venta_nueva_no_pisa_la_de_la_otra_caja(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """POST /sales, chat o carga en lote: ``decrement_for_sale`` valida y
    descuenta sobre el stock que dejó la otra caja, no sobre el cargado antes."""
    pid = await _producto(sm, tenant_id)

    async def vender(s: AsyncSession) -> object:
        venta = SaleEntry(
            tenant_id=tenant_id,
            product_id=pid,
            amount=Decimal("200.00"),
            quantity=2,
            transaction_date=datetime.now(UTC).replace(tzinfo=None),
        )
        s.add(venta)
        await s.flush()
        return await stock_service.decrement_for_sale(venta, s)

    await _carrera(sm, tenant_id, [pid], vender)
    assert await _stock(sm, pid) == (5, 5)


async def test_una_venta_que_ya_no_alcanza_se_rechaza(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """Stock 10, la otra caja vende 3: una venta de 9 ya no alcanza. Con el
    stock leído antes pasaba, y el piso a 0 escondía la sobreventa."""
    pid = await _producto(sm, tenant_id)

    async def vender(s: AsyncSession) -> object:
        with pytest.raises(stock_service.InsufficientStockError):
            await stock_service.check_stock_available(pid, tenant_id, 9, s)
        return None

    await _carrera(sm, tenant_id, [pid], vender)
    assert await _stock(sm, pid) == (7, 7)


async def test_editar_la_cantidad_de_una_venta_no_pisa_la_otra_caja(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """El PATCH valida, repone y descuenta: los tres sobre el stock vigente."""
    pid = await _producto(sm, tenant_id)
    sale_id = await _venta_viva(sm, tenant_id, pid, 2)

    async def editar(s: AsyncSession) -> object:
        venta = await s.get(SaleEntry, sale_id)
        assert venta is not None
        await stock_service.validate_sale_update_stock(sale_id, tenant_id, pid, 2, pid, 4, s)
        await stock_service.revert_sale_stock(sale_id, tenant_id, s)
        venta.quantity = 4
        await s.flush()
        return await stock_service.decrement_for_sale(venta, s)

    await _carrera(sm, tenant_id, [pid], editar)
    assert await _stock(sm, pid) == (5, 5)  # 10 − 3 + 2 − 4


async def test_editar_rechaza_con_el_stock_vigente_y_no_con_el_viejo(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """Stock 10, la venta era de 2 y pasa a 10: con el stock viejo alcanzaba
    (10 + 2); con la caja que vendió 3, no (7 + 2)."""
    pid = await _producto(sm, tenant_id)
    sale_id = await _venta_viva(sm, tenant_id, pid, 2)

    async def editar(s: AsyncSession) -> object:
        with pytest.raises(stock_service.InsufficientStockError):
            await stock_service.validate_sale_update_stock(
                sale_id, tenant_id, pid, 2, pid, 10, s
            )
        return None

    await _carrera(sm, tenant_id, [pid], editar)
    assert await _stock(sm, pid) == (7, 7)


async def test_el_ajuste_de_una_diferencia_mide_el_faltante_con_el_stock_vigente(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """Pendientes 5, stock 4; la otra caja vende 3 → stock 1. El faltante es
    4, no el 1 que daba la lectura vieja (el ticket de prueba no tiene venta
    vinculada, así que después del ajuste no sale nada: 1 + 4)."""
    pid = await _producto(sm, tenant_id, stock=4)
    discrepancia_id = await _discrepancia(sm, tenant_id, pid, pendientes=5)

    async def resolver(s: AsyncSession) -> object:
        discrepancia = await pos_discrepancy_service.obtener(
            s, tenant_id, discrepancia_id, para_actualizar=True
        )
        assert discrepancia is not None
        return await pos_discrepancy_service.aplicar_ajuste_de_entrada(
            s, discrepancia, occurred_at=None
        )

    faltante = await _carrera(sm, tenant_id, [pid], resolver)
    assert faltante == 4
    assert await _stock(sm, pid) == (5, 5)


async def _discrepancia(
    sm: async_sessionmaker[AsyncSession], tid: uuid.UUID, pid: uuid.UUID, *, pendientes: int
) -> uuid.UUID:
    operacion_id, discrepancia_id = uuid.uuid4(), uuid.uuid4()
    async with sm() as s:
        s.add(
            PosOperation(
                id=operacion_id,
                tenant_id=tid,
                client_operation_id=f"op-{uuid.uuid4().hex[:12]}",
                operation_date=datetime.now(UTC).replace(tzinfo=None),
                subtotal_ars=Decimal("500.00"),
                total_ars=Decimal("500.00"),
            )
        )
        await s.flush()
        linea_id = uuid.uuid4()
        s.add(
            PosOperationLine(
                id=linea_id,
                tenant_id=tid,
                operation_id=operacion_id,
                product_id=pid,
                position=0,
                quantity=pendientes,
                unit_price_list=Decimal("100.00"),
                line_total_ars=Decimal("500.00"),
            )
        )
        await s.flush()
        s.add(
            PosStockDiscrepancy(
                id=discrepancia_id,
                tenant_id=tid,
                operation_id=operacion_id,
                line_id=linea_id,
                product_id=pid,
                pending_units=pendientes,
                stock_at_capture=0,
            )
        )
        await s.commit()
    return discrepancia_id


async def test_anular_un_ticket_de_dos_productos_no_se_traba_con_la_otra_caja(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """El ticket repone primero el producto de id MAYOR (su primera línea). La
    caja bloquea en orden de id, como la venta del POS: tiene el menor y pide
    el mayor. Reponiendo en el orden del ticket, cada una espera a la otra y
    Postgres aborta una por deadlock."""
    a, b = sorted([await _producto(sm, tenant_id), await _producto(sm, tenant_id)])
    venta_b = await _venta_viva(sm, tenant_id, b, 1)
    venta_a = await _venta_viva(sm, tenant_id, a, 1)
    operacion_id = uuid.uuid4()
    async with sm() as s:
        user = User(
            user_id=uuid.uuid4(),
            tenant_id=tenant_id,
            email=f"t2-{uuid.uuid4().hex[:8]}@test.com",
            full_name="Dueño",
            password_hash="x",
            role_code="OWNER",
            is_active=True,
        )
        s.add(user)
        s.add(
            PosOperation(
                id=operacion_id,
                tenant_id=tenant_id,
                client_operation_id=f"op-{uuid.uuid4().hex[:12]}",
                operation_date=datetime.now(UTC).replace(tzinfo=None),
                subtotal_ars=Decimal("200.00"),
                total_ars=Decimal("200.00"),
            )
        )
        await s.flush()
        for pos, (pid, venta) in enumerate([(b, venta_b), (a, venta_a)]):
            s.add(
                PosOperationLine(
                    tenant_id=tenant_id,
                    operation_id=operacion_id,
                    product_id=pid,
                    sale_entry_id=venta,
                    position=pos,
                    quantity=1,
                    unit_price_list=Decimal("100.00"),
                    line_total_ars=Decimal("100.00"),
                )
            )
        await s.commit()

    caja = sm()
    await stock_service.bloquear_productos(caja, tenant_id, [a])

    async def anular() -> None:
        async with sm() as s:
            operacion = await s.get(PosOperation, operacion_id)
            lineas = list(
                (
                    await s.execute(
                        select(PosOperationLine).where(
                            PosOperationLine.operation_id == operacion_id
                        )
                    )
                ).scalars()
            )
            assert operacion is not None
            await pos_router._ejecutar_anulacion(
                s,
                tenant_id=tenant_id,
                user=user,
                operacion=operacion,
                lineas=lineas,
                tenders=[],
                reason=None,
                background=BackgroundTasks(),
            )
            await s.commit()

    tarea = asyncio.create_task(anular())
    await asyncio.sleep(0.4)
    # La caja termina su venta: pide el producto de id mayor.
    await stock_service.check_stock_available(b, tenant_id, 1, caja)
    await stock_service.decrement_stock(b, tenant_id, 1, f"pos:{uuid.uuid4()}", caja)
    await caja.commit()
    await caja.close()
    await asyncio.wait_for(tarea, timeout=10)
    assert await _stock(sm, a) == (11, 11)
    assert await _stock(sm, b) == (10, 10)
