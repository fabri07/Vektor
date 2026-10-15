"""M·P3: el costo promedio en cada venta, al lado del costo principal.

Las ventas se costean al confirmarse (listener de ``costeo_de_ventas``) con el
historial publicado; cada publicación recalcula las afectadas. Nunca se muestra
«recalculando»: con el producto pendiente queda el último publicado y
``cost_avg_pendiente``. Sesiones reales que commitean (``fabrica``).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.application.services import costo_promedio_service as svc
from app.application.services import stock_service
from app.jobs import recalcular_costo_promedio as job
from app.persistence import costo_promedio_pendiente as pend
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.product_cost import ProductCostState
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry

D = Decimal


def _dia(n: int) -> datetime:
    return datetime(2026, 3, n, 12, 0)


@pytest.fixture(autouse=True)
def _sin_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pend, "ENCOLAR_EN_HILO", False)
    monkeypatch.setattr(job.recalcular_costo_promedio, "apply_async", lambda *_, **__: None)


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


async def _producto(s: AsyncSession, tid: uuid.UUID, **extra: Any) -> Product:
    p = Product(tenant_id=tid, name="Vela", sale_price_ars=D("300"), stock_units=0, **extra)
    s.add(p)
    await s.flush()
    return p


async def _compra(s: AsyncSession, p: Product, qty: int, costo: str | None, dia: int) -> None:
    await stock_service.increment_stock(
        p.id, p.tenant_id, qty, None if costo is None else D(costo), None, s,
        occurred_at=_dia(dia),
    )


async def _venta(s: AsyncSession, p: Product, qty: int, dia: int) -> uuid.UUID:
    venta = SaleEntry(
        tenant_id=p.tenant_id, product_id=p.id, amount=D("300") * qty, quantity=qty,
        transaction_date=_dia(dia), payment_method="cash",
    )
    s.add(venta)
    await s.flush()
    await stock_service.decrement_stock(
        p.id, p.tenant_id, qty, stock_service.sale_source_event_id(venta.id), s,
        occurred_at=_dia(dia),
    )
    return venta.id


async def _prod(s: AsyncSession, pid: uuid.UUID) -> Product:
    p = await s.get(Product, pid)
    assert p is not None
    return p


async def _leer(fabrica: async_sessionmaker[AsyncSession], sid: uuid.UUID) -> SaleEntry:
    async with fabrica() as s:
        return (await s.execute(select(SaleEntry).where(SaleEntry.id == sid))).scalar_one()


async def _publicar(fabrica: async_sessionmaker[AsyncSession], tid: uuid.UUID) -> svc.Resumen:
    return await svc.recalcular(fabrica, tid, solo_pendientes=True)


async def test_la_venta_guarda_el_promedio_del_dia_sin_tocar_el_costo_principal(
    fabrica: async_sessionmaker[AsyncSession],
) -> None:
    tid = await _tenant(fabrica)
    async with fabrica() as s:
        p = await _producto(s, tid)
        await _compra(s, p, 10, "100", 1)
        await _compra(s, p, 10, "200", 2)
        await s.commit()
        pid = p.id
    await _publicar(fabrica, tid)
    async with fabrica() as s:
        p = await _prod(s, pid)
        sid = await _venta(s, p, 3, 4)
        await s.commit()

    v = await _leer(fabrica, sid)
    assert (v.cost_avg_unit_ars, v.cost_avg_ars) == (D("150.00"), D("450.00"))
    assert (v.cost_avg_estado, v.cost_avg_pendiente, v.cost_avg_aproximado) == (
        "medido", False, False,
    )
    # El costo principal (M·2a) sigue siendo el de reposición: la última compra.
    assert (v.cost_unit_ars, v.cost_origin) == (D("200.00"), "compra")


async def test_compra_y_venta_juntas_dejan_el_publicado_y_la_publicacion_lo_corrige(
    fabrica: async_sessionmaker[AsyncSession],
) -> None:
    """Nunca «recalculando» (decisión del dueño): queda el último promedio publicado
    con la marca interna, y el worker lo pone al día."""
    tid = await _tenant(fabrica)
    async with fabrica() as s:
        p = await _producto(s, tid)
        await _compra(s, p, 10, "100", 1)
        await s.commit()
        pid = p.id
    await _publicar(fabrica, tid)
    async with fabrica() as s:
        p = await _prod(s, pid)
        await _compra(s, p, 10, "200", 3)
        sid = await _venta(s, p, 2, 3)
        await s.commit()

    v = await _leer(fabrica, sid)
    assert (v.cost_avg_unit_ars, v.cost_avg_pendiente) == (D("100.00"), True)

    r = await _publicar(fabrica, tid)
    assert r.ventas >= 1
    v = await _leer(fabrica, sid)
    assert (v.cost_avg_unit_ars, v.cost_avg_ars, v.cost_avg_pendiente) == (
        D("150.00"), D("300.00"), False,
    )


async def test_una_compra_atrasada_recalcula_sólo_las_ventas_posteriores(
    fabrica: async_sessionmaker[AsyncSession],
) -> None:
    tid = await _tenant(fabrica)
    async with fabrica() as s:
        p = await _producto(s, tid)
        await _compra(s, p, 10, "100", 1)
        antes = await _venta(s, p, 1, 2)
        despues = await _venta(s, p, 2, 5)
        await s.commit()
        pid = p.id
    await _publicar(fabrica, tid)
    assert (await _leer(fabrica, despues)).cost_avg_unit_ars == D("100.00")

    async with fabrica() as s:
        p = await _prod(s, pid)
        await _compra(s, p, 9, "200", 3)  # 9 a $200 el día 3, cargada tarde
        await s.commit()
    await _publicar(fabrica, tid)

    assert (await _leer(fabrica, antes)).cost_avg_unit_ars == D("100.00")
    # Día 3: 9 a $100 que quedaban + 9 a $200 → $150.
    v = await _leer(fabrica, despues)
    assert (v.cost_avg_unit_ars, v.cost_avg_ars, v.cost_avg_pendiente) == (
        D("150.00"), D("300.00"), False,
    )


async def test_incompleto_guarda_el_promedio_parcial_con_su_estado(
    fabrica: async_sessionmaker[AsyncSession],
) -> None:
    """Decisión del dueño (2026-10-14): el promedio de las unidades con costo."""
    tid = await _tenant(fabrica)
    async with fabrica() as s:
        p = await _producto(s, tid)
        await _compra(s, p, 4, None, 1)  # sin costo
        await _compra(s, p, 6, "100", 1)
        sid = await _venta(s, p, 2, 2)
        await s.commit()
    await _publicar(fabrica, tid)
    v = await _leer(fabrica, sid)
    assert (v.cost_avg_estado, v.cost_avg_unit_ars, v.cost_avg_ars) == (
        "incompleto", D("100.00"), D("200.00"),
    )


async def test_sin_compras_con_costo_queda_sin_medicion(
    fabrica: async_sessionmaker[AsyncSession],
) -> None:
    tid = await _tenant(fabrica)
    async with fabrica() as s:
        p = await _producto(s, tid)
        await _compra(s, p, 5, None, 1)
        sid = await _venta(s, p, 1, 2)
        await s.commit()
    await _publicar(fabrica, tid)
    v = await _leer(fabrica, sid)
    assert v.cost_avg_unit_ars is None and v.cost_avg_ars is None
    assert v.cost_avg_estado == "sin_medicion"
    assert v.cost_avg_motivo == "hay unidades sin costo en el stock"
    assert v.cost_avg_pendiente is False


async def test_producto_nunca_calculado_queda_pendiente_y_se_completa(
    fabrica: async_sessionmaker[AsyncSession],
) -> None:
    tid = await _tenant(fabrica)
    async with fabrica() as s:
        p = await _producto(s, tid)
        await _compra(s, p, 10, "80", 1)
        sid = await _venta(s, p, 2, 2)
        await s.commit()
    v = await _leer(fabrica, sid)
    assert (v.cost_avg_estado, v.cost_avg_unit_ars, v.cost_avg_pendiente) == (None, None, True)

    await _publicar(fabrica, tid)
    v = await _leer(fabrica, sid)
    assert (v.cost_avg_estado, v.cost_avg_unit_ars, v.cost_avg_pendiente) == (
        "medido", D("80.00"), False,
    )


async def test_venta_por_peso_usa_el_factor(fabrica: async_sessionmaker[AsyncSession]) -> None:
    tid = await _tenant(fabrica)
    async with fabrica() as s:
        p = await _producto(s, tid, sale_unit="gram", base_units_per_sale_unit=1000)
        await _compra(s, p, 5000, "800", 1)  # 5 kg a $800 el kilo
        await s.commit()
        pid = p.id
    await _publicar(fabrica, tid)
    async with fabrica() as s:
        p = await _prod(s, pid)
        sid = await _venta(s, p, 750, 2)
        await s.commit()
    v = await _leer(fabrica, sid)
    assert (v.cost_avg_unit_ars, v.cost_avg_ars) == (D("800.00"), D("600.00"))


async def test_cambiar_la_cantidad_recalcula_la_linea(
    fabrica: async_sessionmaker[AsyncSession],
) -> None:
    tid = await _tenant(fabrica)
    async with fabrica() as s:
        p = await _producto(s, tid)
        await _compra(s, p, 10, "100", 1)
        sid = await _venta(s, p, 2, 2)
        await s.commit()
    await _publicar(fabrica, tid)
    async with fabrica() as s:
        venta = await s.get(SaleEntry, sid)
        assert venta is not None
        venta.quantity = 3
        await s.commit()
    # Al confirmarse, sin esperar la publicación: mismo promedio, nueva línea.
    v = await _leer(fabrica, sid)
    assert (v.cost_avg_unit_ars, v.cost_avg_ars) == (D("100.00"), D("300.00"))
    await _publicar(fabrica, tid)
    v = await _leer(fabrica, sid)
    assert (v.cost_avg_unit_ars, v.cost_avg_ars, v.cost_avg_pendiente) == (
        D("100.00"), D("300.00"), False,
    )


async def test_una_marca_durante_el_calculo_no_deja_la_venta_vieja_para_siempre(
    fabrica: async_sessionmaker[AsyncSession],
) -> None:
    """Revisión de M·P3 (P1): se calcula con una compra atrasada (día 3), entra
    otra marca (día 20) antes de publicar y la publicación se descarta entera,
    historial y ventas. La siguiente publicación, desde el día 3, arregla la
    venta del día 5. Con las ventas fuera de la publicación quedaba en $100 con
    ``pendiente = false`` para siempre (el valor correcto es $147,37)."""
    tid = await _tenant(fabrica)
    async with fabrica() as s:
        p = await _producto(s, tid)
        await _compra(s, p, 10, "100", 1)
        await s.commit()
        pid = p.id
    await _publicar(fabrica, tid)
    async with fabrica() as s:
        sid = await _venta(s, await _prod(s, pid), 1, 5)
        await _compra(s, await _prod(s, pid), 9, "200", 3)
        await s.commit()
    async with fabrica() as s:
        calculo = await svc.calcular(s, tid, [pid])
        await s.rollback()
    async with fabrica() as s:  # otra marca antes de publicar
        await _compra(s, await _prod(s, pid), 1, "300", 20)
        await s.commit()
    async with fabrica() as s:
        publicados, descartados = await svc.publicar(s, calculo)
        await s.commit()
    assert (publicados, descartados) == (set(), {pid})
    assert (await _leer(fabrica, sid)).cost_avg_pendiente is True

    await _publicar(fabrica, tid)
    v = await _leer(fabrica, sid)
    # Día 3: 10 a $100 + 9 a $200 → $2.800 / 19 = $147,37 (la del día 20 no
    # alcanza al día 5).
    assert (v.cost_avg_unit_ars, v.cost_avg_pendiente) == (D("147.37"), False)


async def test_una_republicacion_completa_es_el_backfill_de_las_ventas(
    fabrica: async_sessionmaker[AsyncSession],
) -> None:
    tid = await _tenant(fabrica)
    async with fabrica() as s:
        p = await _producto(s, tid)
        await _compra(s, p, 10, "100", 1)
        sid = await _venta(s, p, 2, 2)
        await s.commit()
    await _publicar(fabrica, tid)
    async with fabrica() as s:  # una venta anterior a M·P3
        await s.execute(
            update(SaleEntry)
            .where(SaleEntry.id == sid)
            .values(cost_avg_unit_ars=None, cost_avg_ars=None, cost_avg_estado=None)
        )
        await s.commit()

    r = await svc.recalcular(fabrica, tid)  # backfill_costo_promedio --all --apply
    assert r.ventas == 1
    v = await _leer(fabrica, sid)
    assert (v.cost_avg_estado, v.cost_avg_unit_ars) == ("medido", D("100.00"))


async def test_la_venta_que_pierde_el_producto_pierde_el_costo_promedio(
    fabrica: async_sessionmaker[AsyncSession],
) -> None:
    tid = await _tenant(fabrica)
    async with fabrica() as s:
        p = await _producto(s, tid)
        await _compra(s, p, 10, "100", 1)
        sid = await _venta(s, p, 2, 2)
        await s.commit()
    await _publicar(fabrica, tid)
    assert (await _leer(fabrica, sid)).cost_avg_unit_ars == D("100.00")
    async with fabrica() as s:
        venta = await s.get(SaleEntry, sid)
        assert venta is not None
        venta.product_id = None
        await s.commit()
    v = await _leer(fabrica, sid)
    assert (v.cost_avg_unit_ars, v.cost_avg_ars, v.cost_avg_estado) == (None, None, None)


async def test_la_venta_omitible_de_un_producto_sin_historial_marca_y_se_completa(
    fabrica: async_sessionmaker[AsyncSession],
) -> None:
    """Revisión de M·P3 (P2): sin nada publicado la venta normal del POS ya no se
    omite; si no, quedaba sin costo promedio hasta el próximo evento."""
    tid = await _tenant(fabrica)
    async with fabrica() as s:  # stock que entró por Core: no marcó nada
        p = await _producto(s, tid)
        p.stock_units = 10
        s.add(
            InventoryMovement(
                tenant_id=tid, product_id=p.id, movement_type="purchase", qty=10,
                unit_cost=D("70"), occurred_at=_dia(1), source_type="purchase_import",
            )
        )
        await s.commit()
        pid = p.id
    async with fabrica() as s:
        await s.execute(update(ProductCostState).where(ProductCostState.product_id == pid)
                        .values(pendiente_desde=None))
        await s.commit()
    async with fabrica() as s:
        sid = await _venta(s, await _prod(s, pid), 1, 2)
        await s.commit()
    assert pid in await _pendientes(fabrica, tid)
    await _publicar(fabrica, tid)
    v = await _leer(fabrica, sid)
    assert (v.cost_avg_unit_ars, v.cost_avg_estado, v.cost_avg_pendiente) == (
        D("70.00"), "medido", False,
    )


async def _pendientes(fabrica: async_sessionmaker[AsyncSession], tid: uuid.UUID) -> list[uuid.UUID]:
    async with fabrica() as s:
        return await svc.productos_pendientes(s, tid)


def test_el_costeo_corre_antes_que_las_marcas_sin_importar_el_orden_de_imports() -> None:
    """Si las marcas se escriben primero, el upsert de ``product_cost_state``
    queda tomado mientras se costea (un import de 5.000 ventas: ~4 s) y el POS
    espera (bench M·P3, medición 5). El orden de registro dependía del orden de
    los imports del proceso: ``insert=True`` lo fija. Se prueba en un intérprete
    aparte que importa el listener del costeo PRIMERO (el orden que lo rompía)."""
    import os  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    import sys  # noqa: PLC0415

    codigo = (
        "import app.persistence.costeo_de_ventas\n"
        "import app.persistence.models\n"
        "from sqlalchemy.orm import Session\n"
        "print(','.join(f.__name__ for f in "
        "Session.dispatch.before_commit._clslevel[Session]))\n"
    )
    salida = subprocess.run(
        [sys.executable, "-c", codigo], capture_output=True, text=True, check=True,
        env={**os.environ},
    ).stdout.strip().splitlines()[-1]
    nombres = salida.split(",")
    assert nombres.index("_costear_al_confirmar") < nombres.index("_marcar_al_confirmar")
