"""M·P2b: el historial del costo promedio se mantiene al día solo.

Qué marca pendiente cada escritura del ledger (y qué se puede omitir), los
savepoints, la publicación con revisión, la lectura «recalculando» y la
recuperación cuando el encolado se pierde. Las compras y ventas se cargan con
los escritores reales de ``stock_service``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.application.services import costo_promedio_service as svc
from app.application.services import stock_service
from app.jobs import recalcular_costo_promedio as job
from app.persistence import costo_promedio_pendiente as pend
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.pos_stock_discrepancy import PosStockDiscrepancy
from app.persistence.models.product import Product
from app.persistence.models.product_cost import ProductCostHistory, ProductCostState
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry

MIN = date.min


def _dia(n: int) -> datetime:
    return datetime(2026, 3, n, 12, 0)


@pytest.fixture
def encolados(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, list[str]]]:
    """Encolado en línea (sin hilo) y capturado."""
    llamadas: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(pend, "ENCOLAR_EN_HILO", False)

    def _apply_async(args: tuple[str, list[str]], **_: Any) -> None:
        llamadas.append((args[0], list(args[1])))

    monkeypatch.setattr(job.recalcular_costo_promedio, "apply_async", _apply_async)
    return llamadas


async def _producto(db: AsyncSession, tenant_id: uuid.UUID, nombre: str = "Vela") -> Product:
    p = Product(tenant_id=tenant_id, name=nombre, sale_price_ars=Decimal("300"), stock_units=0)
    db.add(p)
    await db.flush()
    return p


async def _compra(
    db: AsyncSession, p: Product, qty: int, costo: str, dia: int
) -> InventoryMovement:
    return await stock_service.increment_stock(
        p.id, p.tenant_id, qty, Decimal(costo), None, db, occurred_at=_dia(dia)
    )


async def _venta(
    db: AsyncSession, p: Product, qty: int, dia: int, *, descuenta: bool = True
) -> InventoryMovement | None:
    venta = SaleEntry(
        tenant_id=p.tenant_id, product_id=p.id, amount=Decimal("300") * qty, quantity=qty,
        transaction_date=_dia(dia), payment_method="cash",
    )
    db.add(venta)
    await db.flush()
    if not descuenta:
        return None
    return await stock_service.decrement_stock(
        p.id, p.tenant_id, qty, stock_service.sale_source_event_id(venta.id), db,
        occurred_at=_dia(dia),
    )


async def _estado(db: AsyncSession, p: Product) -> ProductCostState | None:
    return (
        await db.execute(
            select(ProductCostState)
            .where(ProductCostState.product_id == p.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def _al_dia(db: AsyncSession, p: Product) -> None:
    """Publica el historial del producto y deja el estado sin pendientes. Primero
    se confirma lo cargado: la marca de una escritura se aplica en SU commit, así
    que publicar en la misma transacción la vuelve a dejar pendiente."""
    await db.commit()
    await svc.reconstruir(db, p.tenant_id, [p.id])
    await db.commit()
    st = await _estado(db, p)
    assert st is not None and st.pendiente_desde is None


# ── qué marca ───────────────────────────────────────────────────────────────


async def test_una_compra_marca_y_encola(
    db_session: AsyncSession, sample_tenant: Tenant, encolados: list[tuple[str, list[str]]]
) -> None:
    p = await _producto(db_session, sample_tenant.tenant_id)
    await _compra(db_session, p, 10, "100", 3)
    await db_session.commit()

    st = await _estado(db_session, p)
    assert st is not None
    assert (st.pendiente_desde, st.revision) == (date(2026, 3, 3), 1)
    assert st.marcado_en is not None
    assert encolados == [(str(sample_tenant.tenant_id), [str(p.id)])]


async def test_la_venta_normal_del_pos_no_marca(
    db_session: AsyncSession, sample_tenant: Tenant, encolados: list[tuple[str, list[str]]]
) -> None:
    p = await _producto(db_session, sample_tenant.tenant_id)
    await _compra(db_session, p, 10, "100", 1)
    await _al_dia(db_session, p)
    encolados.clear()

    await _venta(db_session, p, 3, 2)
    await db_session.commit()

    st = await _estado(db_session, p)
    assert st is not None and st.pendiente_desde is None
    assert encolados == []


async def test_la_venta_que_deja_el_stock_en_cero_marca(
    db_session: AsyncSession, sample_tenant: Tenant, encolados: list[tuple[str, list[str]]]
) -> None:
    p = await _producto(db_session, sample_tenant.tenant_id)
    await _compra(db_session, p, 5, "100", 1)
    await _al_dia(db_session, p)

    await _venta(db_session, p, 8, 2)  # clamp: el stock queda en 0
    await db_session.commit()

    st = await _estado(db_session, p)
    assert st is not None and st.pendiente_desde == date(2026, 3, 2)


async def test_con_otra_marca_en_el_commit_las_salidas_se_deciden_igual(
    db_session: AsyncSession, sample_tenant: Tenant, encolados: list[tuple[str, list[str]]]
) -> None:
    """Con una compra en el mismo commit, cada salida candidata se decide por
    separado: la venta normal se omite y la que agota el stock marca."""
    tid = sample_tenant.tenant_id
    comprado = await _producto(db_session, tid, "Comprado")
    normal = await _producto(db_session, tid, "Normal")
    agotado = await _producto(db_session, tid, "Agotado")
    for p in (comprado, normal, agotado):
        await _compra(db_session, p, 5, "100", 1)
        await _al_dia(db_session, p)
    encolados.clear()

    await _compra(db_session, comprado, 10, "200", 2)
    await _venta(db_session, normal, 2, 2)
    await _venta(db_session, agotado, 5, 2)
    await db_session.commit()

    assert (await _estado(db_session, comprado)).pendiente_desde == date(2026, 3, 2)  # type: ignore[union-attr]
    assert (await _estado(db_session, normal)).pendiente_desde is None  # type: ignore[union-attr]
    assert (await _estado(db_session, agotado)).pendiente_desde == date(2026, 3, 2)  # type: ignore[union-attr]
    assert sorted(encolados[0][1]) == sorted([str(comprado.id), str(agotado.id)])


async def test_la_venta_anterior_a_una_compra_marca(
    db_session: AsyncSession, sample_tenant: Tenant, encolados: list[tuple[str, list[str]]]
) -> None:
    p = await _producto(db_session, sample_tenant.tenant_id)
    await _compra(db_session, p, 10, "100", 1)
    await _compra(db_session, p, 10, "200", 5)
    await _al_dia(db_session, p)

    await _venta(db_session, p, 2, 3)  # cargada tarde: hay un registro posterior
    await db_session.commit()

    st = await _estado(db_session, p)
    assert st is not None and st.pendiente_desde == date(2026, 3, 3)


async def test_la_venta_sobre_un_registro_incompleto_marca(
    db_session: AsyncSession, sample_tenant: Tenant, encolados: list[tuple[str, list[str]]]
) -> None:
    p = await _producto(db_session, sample_tenant.tenant_id)
    await _compra(db_session, p, 10, "100", 1)
    await stock_service.increment_stock(
        p.id, p.tenant_id, 5, None, None, db_session, occurred_at=_dia(1)
    )
    await _al_dia(db_session, p)

    await _venta(db_session, p, 1, 2)
    await db_session.commit()

    st = await _estado(db_session, p)
    assert st is not None and st.pendiente_desde == date(2026, 3, 2)


async def test_una_venta_sin_movimiento_marca_todo_el_historial(
    db_session: AsyncSession, sample_tenant: Tenant, encolados: list[tuple[str, list[str]]]
) -> None:
    """Una venta manual o del chat sin descontar stock mueve ``aproximado``, que
    está copiado en todo el historial: no alcanza con marcar desde su día."""
    p = await _producto(db_session, sample_tenant.tenant_id)
    await _compra(db_session, p, 10, "100", 1)
    await _al_dia(db_session, p)

    await _venta(db_session, p, 2, 4, descuenta=False)
    await db_session.commit()

    st = await _estado(db_session, p)
    assert st is not None and st.pendiente_desde == MIN


async def test_anular_una_salida_anterior_marca_y_cambia_el_promedio(
    db_session: AsyncSession, sample_tenant: Tenant, encolados: list[tuple[str, list[str]]]
) -> None:
    tid = sample_tenant.tenant_id
    p = await _producto(db_session, tid)
    await _compra(db_session, p, 10, "100", 1)
    salida = await _venta(db_session, p, 5, 2)
    await _compra(db_session, p, 10, "200", 3)
    await _al_dia(db_session, p)
    antes = await svc.costo_a(db_session, tid, p.id, date(2026, 3, 3))
    assert antes is not None and antes.costo_promedio is not None
    assert antes.costo_promedio.quantize(Decimal("0.01")) == Decimal("166.67")

    assert salida is not None
    await stock_service.void_movement(salida, db_session)
    await db_session.commit()

    st = await _estado(db_session, p)
    assert st is not None and st.pendiente_desde == date(2026, 3, 2)
    lectura = await svc.leer_costo_a(db_session, tid, p.id, date(2026, 3, 3))
    assert (lectura.registro, lectura.motivo) == (None, svc.MOTIVO_RECALCULANDO)
    await svc.reconstruir(db_session, tid, [p.id])
    await db_session.commit()
    despues = await svc.leer_costo_a(db_session, tid, p.id, date(2026, 3, 3))
    assert despues.registro is not None and despues.registro.costo_promedio == Decimal("150")


async def test_cambiar_producto_y_fecha_marca_los_dos(
    db_session: AsyncSession, sample_tenant: Tenant, encolados: list[tuple[str, list[str]]]
) -> None:
    tid = sample_tenant.tenant_id
    a = await _producto(db_session, tid, "A")
    b = await _producto(db_session, tid, "B")
    mov = await _compra(db_session, a, 10, "100", 5)
    await _compra(db_session, b, 10, "300", 1)
    await db_session.commit()
    await svc.reconstruir(db_session, tid)
    await db_session.commit()

    mov.product_id = b.id
    mov.occurred_at = _dia(2)
    await db_session.commit()

    sa, sb = await _estado(db_session, a), await _estado(db_session, b)
    assert sa is not None and sa.pendiente_desde == date(2026, 3, 2)
    assert sb is not None and sb.pendiente_desde == date(2026, 3, 2)


async def test_sin_el_valor_anterior_marca_todo_el_historial(
    db_session: AsyncSession, sample_tenant: Tenant, encolados: list[tuple[str, list[str]]]
) -> None:
    tid = sample_tenant.tenant_id
    a = await _producto(db_session, tid, "A")
    b = await _producto(db_session, tid, "B")
    mov = await _compra(db_session, a, 10, "100", 5)
    await _compra(db_session, b, 10, "300", 1)
    await db_session.commit()
    await svc.reconstruir(db_session, tid)
    await db_session.commit()

    db_session.expire(mov, ["product_id"])
    mov.product_id = b.id
    await db_session.commit()

    sb = await _estado(db_session, b)
    assert sb is not None and sb.pendiente_desde == MIN


async def test_un_cambio_de_b11_marca_todo_el_historial(
    db_session: AsyncSession, sample_tenant: Tenant, encolados: list[tuple[str, list[str]]]
) -> None:
    tid = sample_tenant.tenant_id
    p = await _producto(db_session, tid)
    await _compra(db_session, p, 10, "100", 1)
    await _al_dia(db_session, p)

    db_session.add(
        PosStockDiscrepancy(
            tenant_id=tid, operation_id=uuid.uuid4(), line_id=uuid.uuid4(), product_id=p.id,
            pending_units=1, stock_at_capture=0,
        )
    )
    await db_session.commit()

    st = await _estado(db_session, p)
    assert st is not None and st.pendiente_desde == MIN


async def test_un_cambio_de_estado_de_b11_marca(
    db_session: AsyncSession, sample_tenant: Tenant, encolados: list[tuple[str, list[str]]]
) -> None:
    tid = sample_tenant.tenant_id
    p = await _producto(db_session, tid)
    await _compra(db_session, p, 10, "100", 1)
    discrepancia = PosStockDiscrepancy(
        tenant_id=tid, operation_id=uuid.uuid4(), line_id=uuid.uuid4(), product_id=p.id,
        pending_units=1, stock_at_capture=0,
    )
    db_session.add(discrepancia)
    await _al_dia(db_session, p)

    discrepancia.status = "RESOLVED"
    await db_session.commit()

    st = await _estado(db_session, p)
    assert st is not None and st.pendiente_desde == MIN


async def test_reactivar_una_compra_marca(
    db_session: AsyncSession, sample_tenant: Tenant, encolados: list[tuple[str, list[str]]]
) -> None:
    p = await _producto(db_session, sample_tenant.tenant_id)
    mov = await _compra(db_session, p, 10, "100", 4)
    await stock_service.void_movement(mov, db_session)
    await _al_dia(db_session, p)

    await stock_service.unvoid_movement(mov, db_session)
    await db_session.commit()

    st = await _estado(db_session, p)
    assert st is not None and st.pendiente_desde == date(2026, 3, 4)


async def test_cambiar_el_stock_sin_movimiento_marca_todo_el_historial(
    db_session: AsyncSession, sample_tenant: Tenant, encolados: list[tuple[str, list[str]]]
) -> None:
    """Un PATCH de ``stock_units`` (o el chat, o la dedup) mueve
    ``stock_fuera_del_ledger``, que está copiado en todo el historial."""
    p = await _producto(db_session, sample_tenant.tenant_id)
    await _compra(db_session, p, 10, "100", 1)
    await _al_dia(db_session, p)

    p.stock_units = 25
    await db_session.commit()

    st = await _estado(db_session, p)
    assert st is not None and st.pendiente_desde == MIN


async def test_una_venta_normal_con_el_estado_pendiente_marca(
    db_session: AsyncSession, sample_tenant: Tenant, encolados: list[tuple[str, list[str]]]
) -> None:
    """Con algo pendiente, lo publicado ya está en revisión: la omisión no se
    decide sobre un historial que puede estar viejo."""
    p = await _producto(db_session, sample_tenant.tenant_id)
    await _compra(db_session, p, 10, "100", 1)
    await _al_dia(db_session, p)
    await _compra(db_session, p, 10, "200", 2)
    await db_session.commit()

    await _venta(db_session, p, 1, 3)
    await db_session.commit()

    st = await _estado(db_session, p)
    assert st is not None and st.pendiente_desde == date(2026, 3, 2)
    assert st.revision == 4  # publicación (2), compra (3) y la venta (4)


async def test_pendiente_sin_nada_publicado_la_venta_marca_igual(
    db_session: AsyncSession, sample_tenant: Tenant, encolados: list[tuple[str, list[str]]]
) -> None:
    """Producto nuevo: la compra lo deja pendiente y el job puede estar
    calculando la primera publicación. Si la venta no subiera la revisión, esa
    publicación saldría sin ella y quedaría como válida."""
    p = await _producto(db_session, sample_tenant.tenant_id)
    await _compra(db_session, p, 10, "100", 1)
    await db_session.commit()

    await _venta(db_session, p, 1, 2)
    await db_session.commit()

    st = await _estado(db_session, p)
    assert st is not None and st.pendiente_desde == date(2026, 3, 1)
    assert st.revision == 2  # compra (1) y la venta (2)


async def test_el_helper_de_las_escrituras_core_marca_todo(
    db_session: AsyncSession, sample_tenant: Tenant, encolados: list[tuple[str, list[str]]]
) -> None:
    tid = sample_tenant.tenant_id
    p = await _producto(db_session, tid)
    await _compra(db_session, p, 10, "100", 1)
    await _al_dia(db_session, p)

    pend.marcar_todo_el_historial(db_session, tid, [p.id, None])
    await db_session.commit()

    st = await _estado(db_session, p)
    assert st is not None and st.pendiente_desde == MIN


# ── savepoints y rollback ───────────────────────────────────────────────────


async def test_un_savepoint_revertido_no_borra_las_marcas_de_afuera(
    db_session: AsyncSession, sample_tenant: Tenant, encolados: list[tuple[str, list[str]]]
) -> None:
    tid = sample_tenant.tenant_id
    a = await _producto(db_session, tid, "A")
    b = await _producto(db_session, tid, "B")
    await _compra(db_session, a, 10, "100", 2)
    sp = await db_session.begin_nested()
    await _compra(db_session, b, 10, "300", 1)
    await sp.rollback()
    await db_session.commit()

    sa = await _estado(db_session, a)
    assert sa is not None and sa.pendiente_desde == date(2026, 3, 2)


async def test_un_rollback_total_no_marca(
    db_session: AsyncSession, sample_tenant: Tenant, encolados: list[tuple[str, list[str]]]
) -> None:
    p = await _producto(db_session, sample_tenant.tenant_id)
    await db_session.commit()
    pid = p.id
    await _compra(db_session, p, 10, "100", 1)
    await db_session.rollback()
    await db_session.commit()

    estado = (
        await db_session.execute(
            select(ProductCostState).where(ProductCostState.product_id == pid)
        )
    ).scalar_one_or_none()
    assert estado is None
    assert encolados == []


async def test_liberar_un_savepoint_no_encola(
    db_session: AsyncSession, sample_tenant: Tenant, encolados: list[tuple[str, list[str]]]
) -> None:
    p = await _producto(db_session, sample_tenant.tenant_id)
    sp = await db_session.begin_nested()
    await _compra(db_session, p, 10, "100", 1)
    await sp.commit()
    assert encolados == []
    await db_session.commit()
    assert encolados == [(str(sample_tenant.tenant_id), [str(p.id)])]


# ── publicación, drenaje y verificación (commits reales) ────────────────────


@pytest_asyncio.fixture
async def fabrica(
    isolated_db_engine: AsyncEngine,
) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    yield async_sessionmaker(isolated_db_engine, expire_on_commit=False)


async def _tenant(fabrica: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    tid = uuid.uuid4()
    async with fabrica() as s:
        s.add(Tenant(tenant_id=tid, legal_name="T", display_name="T"))
        await s.commit()
    return tid


async def test_recuperacion_tras_perder_el_encolado(
    fabrica: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    def _broker_caido(*_: Any, **__: Any) -> None:
        raise ConnectionError("broker caído")

    monkeypatch.setattr(pend, "ENCOLAR_EN_HILO", False)
    monkeypatch.setattr(job.recalcular_costo_promedio, "apply_async", _broker_caido)
    tid = await _tenant(fabrica)
    async with fabrica() as s:
        p = await _producto(s, tid)
        await _compra(s, p, 10, "100", 1)
        await _venta(s, p, 5, 2)
        await _compra(s, p, 10, "200", 3)
        await s.commit()  # el encolado falla y no se propaga
        pid = p.id

    async with fabrica() as s:
        assert await svc.productos_pendientes(s, tid) == [pid]

    r = await svc.recalcular(fabrica, tid, solo_pendientes=True)

    assert (r.publicados, r.descartados) == (1, 0)
    async with fabrica() as s:
        assert await svc.productos_pendientes(s, tid) == []
        lectura = await svc.leer_costo_a(s, tid, pid, date(2026, 3, 3))
        assert lectura.registro is not None
        assert lectura.registro.costo_promedio is not None
        assert lectura.registro.costo_promedio.quantize(Decimal("0.01")) == Decimal("166.67")
        assert await svc.verificar(s, tid) == []


async def test_recalcular_da_lo_mismo_que_reconstruir(
    fabrica: async_sessionmaker[AsyncSession], encolados: list[tuple[str, list[str]]]
) -> None:
    tid = await _tenant(fabrica)
    async with fabrica() as s:
        p = await _producto(s, tid)
        await _compra(s, p, 10, "100", 1)
        await _venta(s, p, 4, 2)
        await _compra(s, p, 6, "150", 3)
        await stock_service.increment_stock(p.id, tid, 3, None, None, s, occurred_at=_dia(4))
        await s.commit()

    await svc.recalcular(fabrica, tid)
    async with fabrica() as s:
        via_job = [
            (r.dia, r.clase, r.costo_promedio, r.estado)
            for r in (
                await s.execute(select(ProductCostHistory).order_by(ProductCostHistory.dia))
            ).scalars()
        ]
        await svc.reconstruir(s, tid)
        await s.commit()
        via_backfill = [
            (r.dia, r.clase, r.costo_promedio, r.estado)
            for r in (
                await s.execute(select(ProductCostHistory).order_by(ProductCostHistory.dia))
            ).scalars()
        ]
    assert via_job == via_backfill and len(via_job) == 3


async def test_una_marca_durante_el_calculo_descarta_la_publicacion(
    fabrica: async_sessionmaker[AsyncSession], encolados: list[tuple[str, list[str]]]
) -> None:
    """El contrato de revisión, en SQLite: se calcula, entra una marca y la
    publicación no escribe nada; el producto sigue pendiente."""
    tid = await _tenant(fabrica)
    async with fabrica() as s:
        p = await _producto(s, tid)
        await _compra(s, p, 10, "100", 1)
        await s.commit()
        pid = p.id

    async with fabrica() as s:
        calculo = await svc.calcular(s, tid, [pid])
    async with fabrica() as s:
        prod = await s.get(Product, pid)
        assert prod is not None
        await _compra(s, prod, 10, "200", 2)
        await s.commit()
    async with fabrica() as s:
        publicados, descartados = await svc.publicar(s, calculo)
        await s.commit()
    assert (publicados, descartados) == (set(), {pid})
    async with fabrica() as s:
        assert await svc.productos_pendientes(s, tid) == [pid]
        assert (await s.execute(select(ProductCostHistory))).scalars().all() == []


async def test_el_job_drena_y_registra_los_fallos(
    fabrica: async_sessionmaker[AsyncSession],
    encolados: list[tuple[str, list[str]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = await _tenant(fabrica)
    async with fabrica() as s:
        a = await _producto(s, tid, "A")
        b = await _producto(s, tid, "B")
        await _compra(s, a, 10, "100", 1)
        await _compra(s, b, 10, "300", 1)
        await s.commit()
        pa, pb = a.id, b.id
    # B quedó viejo: marcado hace rato, como si su encolado se hubiera perdido.
    async with fabrica() as s:
        st = (
            await s.execute(select(ProductCostState).where(ProductCostState.product_id == pb))
        ).scalar_one()
        st.marcado_en = datetime(2026, 1, 1)
        await s.commit()

    reencolar, contadores, error = await job.procesar(fabrica, tid, [pa])

    assert (reencolar, error) == (False, None)
    assert contadores == {
        "publicados": 1, "descartados": 0, "drenados": 1, "drenaje_descartados": 0
    }
    async with fabrica() as s:
        assert await svc.productos_pendientes(s, tid) == []

    async def _falla(*_: Any, **__: Any) -> Any:
        raise RuntimeError("se cayó la base")

    async with fabrica() as s:
        prod = await s.get(Product, pa)
        assert prod is not None
        await _compra(s, prod, 5, "150", 2)
        await s.commit()
    monkeypatch.setattr(svc, "recalcular", _falla)
    _, _, error = await job.procesar(fabrica, tid, [pa])
    assert error is not None and "se cayó la base" in error
    async with fabrica() as s:
        st = (
            await s.execute(select(ProductCostState).where(ProductCostState.product_id == pa))
        ).scalar_one()
        assert st.intentos == 1 and st.ultimo_error is not None
        assert st.pendiente_desde == date(2026, 3, 2)


async def test_la_publicacion_parcial_da_lo_mismo_que_la_completa(
    fabrica: async_sessionmaker[AsyncSession], encolados: list[tuple[str, list[str]]]
) -> None:
    """Con algo pendiente desde un día, sólo se reemplazan los registros desde ese
    día. Después de varias publicaciones parciales (una compra al final, una venta
    con fecha vieja, otra compra), el historial es el mismo que el de una
    reconstrucción completa."""
    tid = await _tenant(fabrica)
    async with fabrica() as s:
        p = await _producto(s, tid)
        for dia, costo in ((1, "100"), (3, "120"), (5, "150")):
            await _compra(s, p, 10, costo, dia)
        await s.commit()
        pid = p.id
    await svc.recalcular(fabrica, tid)

    async def _cambiar(
        dia: int, *, compra: str | None = None, venta: int = 0, hora: int = 12
    ) -> None:
        async with fabrica() as s:
            prod = await s.get(Product, pid)
            assert prod is not None
            if compra is not None:
                await stock_service.increment_stock(
                    pid, tid, 10, Decimal(compra), None, s,
                    occurred_at=datetime(2026, 3, dia, hora),
                )
            if venta:
                await _venta(s, prod, venta, dia)
            await s.commit()
        async with fabrica() as s:
            st = await _estado(s, prod)
            assert st is not None and st.pendiente_desde == date(2026, 3, dia)
        r = await svc.recalcular(fabrica, tid, [pid], solo_pendientes=True)
        assert r.publicados == 1

    await _cambiar(7, compra="200")
    await _cambiar(2, venta=4)  # cargada tarde: hay registros posteriores
    await _cambiar(8, compra="210")
    # A la mañana del día 3: queda ANTES del registro de las 12 ya publicado y
    # lo cambia. El reemplazo tiene que incluir el día pendiente entero.
    await _cambiar(3, compra="90", hora=8)

    async with fabrica() as s:
        assert await svc.verificar(s, tid) == []
        parcial = [
            (r.dia, r.costo_promedio)
            for r in (
                await s.execute(select(ProductCostHistory).order_by(ProductCostHistory.dia))
            ).scalars()
        ]
    async with fabrica() as s:
        await svc.reconstruir(s, tid)
        await s.commit()
        completo = [
            (r.dia, r.costo_promedio)
            for r in (
                await s.execute(select(ProductCostHistory).order_by(ProductCostHistory.dia))
            ).scalars()
        ]
    assert parcial == completo and len(completo) == 6


async def test_verificar_detecta_un_historial_viejo(
    fabrica: async_sessionmaker[AsyncSession], encolados: list[tuple[str, list[str]]]
) -> None:
    tid = await _tenant(fabrica)
    async with fabrica() as s:
        p = await _producto(s, tid)
        await _compra(s, p, 10, "100", 1)
        await s.commit()
        pid = p.id
    await svc.recalcular(fabrica, tid)
    async with fabrica() as s:
        reg = (await s.execute(select(ProductCostHistory))).scalar_one()
        reg.costo_promedio = Decimal("999")
        await s.commit()
        assert await svc.verificar(s, tid) == [pid]
