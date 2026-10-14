"""M·P2b contra Postgres: el contrato de publicación bajo concurrencia real.

SQLite no tiene ``REPEATABLE READ``, ni advisory locks, ni bloqueo de fila: acá
cada caso usa sesiones (conexiones) distintas que commitean de verdad.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator
from datetime import date, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.application.services import costo_promedio_service as svc
from app.application.services import stock_service
from app.jobs import recalcular_costo_promedio as job
from app.persistence.models.inventory import InventoryBalance, InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.product_cost import ProductCostHistory, ProductCostState
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.tests.integration import _reread_scenario as esc

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere TEST_PG_DSN (Postgres real)"),
]

_DDL_ADVISORY_KEY = 0x5645_4B54_4F52_4D32  # "VEKTOR" + M2


@pytest_asyncio.fixture(scope="module")
async def pg_engine() -> AsyncGenerator[AsyncEngine, None]:
    assert TEST_PG_DSN is not None
    engine = await esc.crear_engine(TEST_PG_DSN, _DDL_ADVISORY_KEY)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def fabrica(pg_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(pg_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _sin_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(job.recalcular_costo_promedio, "apply_async", lambda *_, **__: None)


@pytest_asyncio.fixture
async def producto(
    fabrica: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[tuple[uuid.UUID, uuid.UUID], None]:
    """Un tenant con un producto: compra 10 a $100 el día 1, publicado."""
    tid = uuid.uuid4()
    async with fabrica() as s:
        s.add(Tenant(tenant_id=tid, legal_name="MP2b", display_name="MP2b"))
        await s.flush()
        p = Product(tenant_id=tid, name="Vela", sale_price_ars=Decimal("300"), stock_units=0)
        s.add(p)
        await s.flush()
        await stock_service.increment_stock(
            p.id, tid, 10, Decimal("100"), None, s, occurred_at=datetime(2026, 3, 1, 12)
        )
        await s.commit()
        pid = p.id
    await svc.recalcular(fabrica, tid)
    try:
        yield tid, pid
    finally:
        async with fabrica() as s:
            for modelo in (
                ProductCostHistory, ProductCostState, InventoryMovement, InventoryBalance,
                SaleEntry, Product,
            ):
                await s.execute(delete(modelo).where(modelo.tenant_id == tid))
            await s.execute(delete(Tenant).where(Tenant.tenant_id == tid))
            await s.commit()


async def _vender(
    fabrica: async_sessionmaker[AsyncSession], tid: uuid.UUID, pid: uuid.UUID, qty: int, dia: int
) -> None:
    async with fabrica() as s:
        venta = SaleEntry(
            tenant_id=tid, product_id=pid, amount=Decimal("300") * qty, quantity=qty,
            transaction_date=datetime(2026, 3, dia, 12), payment_method="cash",
        )
        s.add(venta)
        await s.flush()
        await stock_service.decrement_stock(
            pid, tid, qty, stock_service.sale_source_event_id(venta.id), s,
            occurred_at=datetime(2026, 3, dia, 12),
        )
        await s.commit()


async def _comprar(
    fabrica: async_sessionmaker[AsyncSession], tid: uuid.UUID, pid: uuid.UUID, costo: str, dia: int
) -> None:
    async with fabrica() as s:
        await stock_service.increment_stock(
            pid, tid, 10, Decimal(costo), None, s, occurred_at=datetime(2026, 3, dia, 12)
        )
        await s.commit()


async def _calcular_en_foto(
    fabrica: async_sessionmaker[AsyncSession], tid: uuid.UUID, pid: uuid.UUID
) -> svc.Calculo:
    async with fabrica() as s:
        await s.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        calculo = await svc.calcular(s, tid, [pid])
        await s.rollback()
    return calculo


async def _publicar(fabrica: async_sessionmaker[AsyncSession], calculo: svc.Calculo) -> tuple[
    set[uuid.UUID], set[uuid.UUID]
]:
    async with fabrica() as s:
        resultado = await svc.publicar(s, calculo)
        await s.commit()
    return resultado


async def _estado(fabrica: async_sessionmaker[AsyncSession], pid: uuid.UUID) -> ProductCostState:
    async with fabrica() as s:
        return (
            await s.execute(select(ProductCostState).where(ProductCostState.product_id == pid))
        ).scalar_one()


async def test_un_calculo_atrasado_no_pisa_una_marca(
    fabrica: async_sessionmaker[AsyncSession], producto: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tid, pid = producto
    calculo = await _calcular_en_foto(fabrica, tid, pid)
    await _comprar(fabrica, tid, pid, "200", 2)  # marca entre el cálculo y la publicación

    publicados, descartados = await _publicar(fabrica, calculo)

    assert (publicados, descartados) == (set(), {pid})
    assert (await _estado(fabrica, pid)).pendiente_desde == date(2026, 3, 2)


async def test_una_salida_omitida_durante_el_calculo_no_cambia_lo_publicado(
    fabrica: async_sessionmaker[AsyncSession], producto: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tid, pid = producto
    calculo = await _calcular_en_foto(fabrica, tid, pid)
    await _vender(fabrica, tid, pid, 3, 2)  # normal, stock > 0: no marca

    publicados, _ = await _publicar(fabrica, calculo)

    assert publicados == {pid}
    async with fabrica() as s:
        assert await svc.verificar(s, tid) == []


async def test_una_salida_que_deja_el_stock_en_cero_invalida(
    fabrica: async_sessionmaker[AsyncSession], producto: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tid, pid = producto
    calculo = await _calcular_en_foto(fabrica, tid, pid)
    await _vender(fabrica, tid, pid, 12, 2)  # clamp en 0: marca

    publicados, descartados = await _publicar(fabrica, calculo)

    assert (publicados, descartados) == (set(), {pid})


async def test_dos_publicaciones_a_la_vez_gana_una(
    fabrica: async_sessionmaker[AsyncSession], producto: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tid, pid = producto
    await _comprar(fabrica, tid, pid, "200", 2)
    a = await _calcular_en_foto(fabrica, tid, pid)
    b = await _calcular_en_foto(fabrica, tid, pid)

    resultados = await asyncio.gather(_publicar(fabrica, a), _publicar(fabrica, b))

    assert sorted(len(pub) for pub, _ in resultados) == [0, 1]
    assert (await _estado(fabrica, pid)).pendiente_desde is None


async def test_primera_marca_contra_primera_reconstruccion(
    fabrica: async_sessionmaker[AsyncSession], producto: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Sin fila de estado: el cálculo lee revisión 0; una marca crea la fila con
    revisión 1 antes de publicar; la publicación la ve y descarta."""
    tid, pid = producto
    async with fabrica() as s:
        await s.execute(delete(ProductCostState).where(ProductCostState.product_id == pid))
        await s.commit()
    calculo = await _calcular_en_foto(fabrica, tid, pid)
    assert calculo.revisiones[pid] == 0
    await _comprar(fabrica, tid, pid, "200", 2)

    publicados, descartados = await _publicar(fabrica, calculo)

    assert (publicados, descartados) == (set(), {pid})
    assert (await _estado(fabrica, pid)).revision == 1


async def test_una_venta_del_pos_durante_la_publicacion_conserva_su_marca(
    fabrica: async_sessionmaker[AsyncSession], producto: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tid, pid = producto
    await _comprar(fabrica, tid, pid, "200", 2)
    calculo = await _calcular_en_foto(fabrica, tid, pid)
    async with fabrica() as publicador:
        publicados, _ = await svc.publicar(publicador, calculo)
        assert publicados == {pid}
        # La venta deja el stock en 0 (marca) y espera el lock de la fila.
        venta = asyncio.create_task(_vender(fabrica, tid, pid, 20, 3))
        await asyncio.sleep(0.3)
        assert not venta.done()
        await publicador.commit()
    await asyncio.wait_for(venta, timeout=10)

    st = await _estado(fabrica, pid)
    assert st.pendiente_desde == date(2026, 3, 3)
