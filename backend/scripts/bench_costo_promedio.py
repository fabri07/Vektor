"""Benchmark de M·P2b: el costo promedio al día sin frenar al POS.

Mide contra un Postgres LOCAL (se niega a correr contra otro host), en un tenant
descartable que borra al final:

1. Reconstrucción completa de un negocio (``--productos`` × ``--movs``).
2. Recálculo de un producto con ``--eventos-grande`` eventos.
3. Latencia que el listener le agrega al commit de una venta del POS, con la
   salida omitida (la venta normal) y sin omitir (marca).
4. Espera del POS por locks mientras se recalcula el mismo producto.
5. Import de ``--ventas-import`` ventas históricas (otro proceso) mientras el POS
   vende uno de los productos que marca, con recálculos de fondo.

Presupuestos (plan M·P2b, ajustados por el dueño el 2026-10-13): 1 ≤ 120 s ·
2a (compra nueva) ≤ 2 s · 2b (todo el historial) ≤ 4 s · 3 venta omitida p95 ≤ 10 ms,
venta que marca p95 ≤ 15 ms, p99 ≤ 25 ms · 4 p99 ≤ 50 ms · 5 sin deadlocks y
p99 ≤ 50 ms. La medición 3 y la 4 cronometran el tiempo DENTRO del listener (sus
consultas incluidas): restar percentiles de dos commits con fsync es ruido.
Imprime una tabla con OK/NO.

Usage (desde ``backend/``):
    DATABASE_URL='postgresql://vektor:vektor@localhost:5432/vektor_pgtest' \\
        .venv/bin/python scripts/bench_costo_promedio.py
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import multiprocessing
import os
import statistics
import sys
import time
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPTS)
sys.path.insert(0, os.path.dirname(_SCRIPTS))

from _db import async_engine_config  # noqa: E402
from sqlalchemy import delete, event, insert, update  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.application.agents.shared.event_bus import EventBus  # noqa: E402
from app.application.services import costo_promedio_service as svc  # noqa: E402
from app.application.services import stock_service  # noqa: E402
from app.jobs import recalcular_costo_promedio as job  # noqa: E402
from app.persistence import costo_promedio_pendiente as pend  # noqa: E402
from app.persistence.db.engine_factory import crear_engine  # noqa: E402
from app.persistence.models.inventory import InventoryBalance, InventoryMovement  # noqa: E402
from app.persistence.models.product import Product  # noqa: E402
from app.persistence.models.product_cost import (  # noqa: E402
    ProductCostHistory,
    ProductCostState,
)
from app.persistence.models.tenant import Tenant  # noqa: E402
from app.persistence.models.transaction import SaleEntry  # noqa: E402

BASE = datetime(2024, 1, 1, 12, 0)


def _pct(xs: list[float], q: float) -> float:
    ordenadas = sorted(xs)
    return ordenadas[min(len(ordenadas) - 1, int(len(ordenadas) * q))]


def _movimientos(tid: uuid.UUID, pid: uuid.UUID, n: int) -> tuple[list[dict[str, Any]], int]:
    """Compras de 10 a precio creciente y ventas de 5, alternadas, un día cada una."""
    filas, stock = [], 0
    for i in range(n):
        dia = BASE + timedelta(days=i)
        if i % 2 == 0:
            filas.append({
                "id": uuid.uuid4(), "tenant_id": tid, "product_id": pid,
                "movement_type": "purchase", "qty": 10,
                "unit_cost": Decimal(100 + i), "occurred_at": dia,
                "source_type": "purchase_import",
            })
            stock += 10
        else:
            filas.append({
                "id": uuid.uuid4(), "tenant_id": tid, "product_id": pid,
                "movement_type": "sale", "qty": -5, "unit_cost": None, "occurred_at": dia,
                "source_event_id": f"sale:{uuid.uuid4()}",
            })
            stock -= 5
    return filas, stock


async def _crear(
    fabrica: async_sessionmaker[AsyncSession],
    tid: uuid.UUID,
    productos: int,
    movs: int,
    *,
    con_tenant: bool = True,
) -> list[uuid.UUID]:
    """Carga masiva por Core (no pasa por el listener: es el estado inicial)."""
    ids = [uuid.uuid4() for _ in range(productos)]
    async with fabrica() as s:
        if con_tenant:
            s.add(Tenant(tenant_id=tid, legal_name="bench", display_name="bench M·P2b"))
            await s.flush()
        filas_p, filas_m = [], []
        for pid in ids:
            m, stock = _movimientos(tid, pid, movs)
            filas_m.extend(m)
            filas_p.append({
                "id": pid, "tenant_id": tid, "name": f"P {pid.hex[:8]}",
                "name_normalized": f"p {pid.hex[:8]}",
                "sale_price_ars": Decimal("1000"), "stock_units": stock,
            })
        for i in range(0, len(filas_p), 2000):
            await s.execute(insert(Product), filas_p[i : i + 2000])
        for i in range(0, len(filas_m), 5000):
            await s.execute(insert(InventoryMovement), filas_m[i : i + 5000])
        await s.commit()
    return ids


async def _vender(
    fabrica: async_sessionmaker[AsyncSession], tid: uuid.UUID, pid: uuid.UUID, qty: int = 1
) -> float:
    """Una venta del POS (venta + su salida de stock) y lo que tarda su commit."""
    async with fabrica() as s:
        venta = SaleEntry(
            tenant_id=tid, product_id=pid, amount=Decimal("1000") * qty, quantity=qty,
            transaction_date=datetime.now(), payment_method="cash",
        )
        s.add(venta)
        await s.flush()
        await stock_service.decrement_stock(
            pid, tid, qty, stock_service.sale_source_event_id(venta.id), s
        )
        t0 = time.perf_counter()
        await s.commit()
        return (time.perf_counter() - t0) * 1000


_EN_HOOKS = [0.0]  # ms dentro de los hooks del listener desde la última puesta a cero


def _cronometrado(fn: Any) -> Any:
    def envoltura(*a: Any, **k: Any) -> Any:
        t0 = time.perf_counter()
        try:
            return fn(*a, **k)
        finally:
            _EN_HOOKS[0] += (time.perf_counter() - t0) * 1000

    return envoltura


_HOOKS = (
    ("after_flush", pend._anotar, _cronometrado(pend._anotar)),
    ("before_commit", pend._marcar_al_confirmar, _cronometrado(pend._marcar_al_confirmar)),
    ("after_commit", pend._encolar, _cronometrado(pend._encolar)),
)


def _listener(activo: bool) -> None:
    """Prende o apaga el listener. Prendido, corre una versión cronometrada de
    cada hook: su tiempo (consultas a la base incluidas) es lo que el listener
    le agrega a la venta, sin restar percentiles de dos colas con fsync."""
    for nombre, original, medido in _HOOKS:
        if event.contains(Session, nombre, original):
            event.remove(Session, nombre, original)
        if activo and not event.contains(Session, nombre, medido):
            event.listen(Session, nombre, medido)
        elif not activo and event.contains(Session, nombre, medido):
            event.remove(Session, nombre, medido)


async def _vender_medido(
    fabrica: async_sessionmaker[AsyncSession], tid: uuid.UUID, pid: uuid.UUID
) -> tuple[float, float]:
    """``(commit, hooks)`` en ms: lo que tarda el commit y lo que pasó dentro del
    listener en toda la venta (flushes y commit)."""
    _EN_HOOKS[0] = 0.0
    commit = await _vender(fabrica, tid, pid)
    return commit, _EN_HOOKS[0]


async def _bucle(
    url: str, connect_args: dict[str, Any], tid: uuid.UUID, pids: list[uuid.UUID],
    parar: Any, vueltas: Any,
) -> None:
    job.recalcular_costo_promedio.apply_async = lambda *_, **__: None
    engine = crear_engine(url, connect_args=connect_args, echo=False)
    fabrica = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        while not parar.is_set():
            for pid in pids:
                async with fabrica() as s:
                    await s.execute(
                        update(ProductCostState)
                        .where(ProductCostState.product_id == pid)
                        .values(pendiente_desde=BASE.date(), revision=ProductCostState.revision + 1)
                    )
                    await s.commit()
                await svc.recalcular(fabrica, tid, [pid], solo_pendientes=True)
                with vueltas.get_lock():
                    vueltas.value += 1
    finally:
        await engine.dispose()


def _proceso(
    url: str, connect_args: dict[str, Any], tid: uuid.UUID, pids: list[uuid.UUID],
    parar: Any, vueltas: Any,
) -> None:
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    asyncio.run(_bucle(url, connect_args, tid, pids, parar, vueltas))


class _Recalculos:
    """Recálculos en bucle en OTRO proceso, como el worker de Celery: si corrieran
    en el event loop de las ventas, el cálculo en Python las frenaría sin que
    haya ningún lock de por medio."""

    def __init__(
        self, url: str, connect_args: dict[str, Any], tid: uuid.UUID, pids: list[uuid.UUID]
    ) -> None:
        ctx = multiprocessing.get_context("spawn")
        self._parar = ctx.Event()
        self._vueltas = ctx.Value("i", 0)
        self._p = ctx.Process(
            target=_proceso, args=(url, connect_args, tid, pids, self._parar, self._vueltas)
        )
        self._p.start()
        time.sleep(2)  # que arranque y empiece a recalcular

    def parar(self) -> int:
        """Las vueltas hechas; ``-1`` si el proceso murió (un ``DeadlockDetected``
        en ``publicar`` lo mata) o no terminó a tiempo."""
        self._parar.set()
        self._p.join(timeout=60)
        if self._p.is_alive():
            self._p.terminate()
            self._p.join()
            return -1
        return int(self._vueltas.value) if self._p.exitcode == 0 else -1


async def _importar(
    url: str, connect_args: dict[str, Any], tid: uuid.UUID, pids: list[uuid.UUID], n: int,
    duracion: Any, ok: Any,
) -> None:
    job.recalcular_costo_promedio.apply_async = lambda *_, **__: None
    engine = crear_engine(url, connect_args=connect_args, echo=False)
    fabrica = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with fabrica() as s:
            for i in range(n):
                s.add(
                    SaleEntry(
                        tenant_id=tid, product_id=pids[i % len(pids)],
                        amount=Decimal("1000"), quantity=1,
                        transaction_date=BASE + timedelta(days=i % 30),
                        payment_method="cash",
                    )
                )
            t0 = time.perf_counter()
            await s.commit()
            duracion.value = time.perf_counter() - t0
            ok.value = 1
    finally:
        await engine.dispose()


def _proceso_import(
    url: str, connect_args: dict[str, Any], tid: uuid.UUID, pids: list[uuid.UUID], n: int,
    duracion: Any, ok: Any,
) -> None:
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    asyncio.run(_importar(url, connect_args, tid, pids, n, duracion, ok))


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--productos", type=int, default=10_000)
    ap.add_argument("--movs", type=int, default=20)
    ap.add_argument("--eventos-grande", type=int, default=50_000)
    ap.add_argument("--ventas-pos", type=int, default=300)
    ap.add_argument("--ventas-import", type=int, default=5_000)
    args = ap.parse_args()

    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    url, connect_args = async_engine_config()
    host = urlparse(url).hostname
    if host not in ("localhost", "127.0.0.1"):
        print(f"ERROR: el benchmark sólo corre contra un Postgres local (host={host}).")
        sys.exit(2)
    job.recalcular_costo_promedio.apply_async = lambda *_, **__: None  # sin broker: mide la base
    # El event bus de stock tampoco: sin broker, kombu reintenta ~0,7 s por
    # emisión. Cae antes del commit (no cambia lo medido), pero alarga la corrida.
    EventBus.emit = staticmethod(lambda *_, **__: None)  # type: ignore[method-assign]
    engine = crear_engine(url, connect_args=connect_args, echo=False, pool_size=10)
    fabrica = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    tid = uuid.uuid4()
    filas: list[tuple[str, str, str, bool]] = []
    try:
        print(f"Cargando {args.productos} productos × {args.movs} movimientos…")
        ids = await _crear(fabrica, tid, args.productos, args.movs)

        t0 = time.perf_counter()
        r = await svc.recalcular(fabrica, tid)
        dur = time.perf_counter() - t0
        filas.append((
            "1. reconstrucción completa",
            f"{dur:.1f} s ({r.publicados} productos, {r.registros} registros)",
            "≤ 120 s", dur <= 120,
        ))

        grande = (await _crear(fabrica, tid, 1, args.eventos_grande, con_tenant=False))[0]
        await svc.recalcular(fabrica, tid, [grande])
        # a) Lo de todos los días: una compra nueva al final → publicación parcial.
        async with fabrica() as s:
            await stock_service.increment_stock(
                grande, tid, 10, Decimal("999"), None, s,
                occurred_at=BASE + timedelta(days=args.eventos_grande + 1),
            )
            await s.commit()
        t0 = time.perf_counter()
        await svc.recalcular(fabrica, tid, [grande], solo_pendientes=True)
        dur = time.perf_counter() - t0
        filas.append((
            f"2a. recálculo de 1 producto ({args.eventos_grande} eventos), compra nueva",
            f"{dur:.2f} s", "≤ 2 s", dur <= 2,
        ))
        # b) Todo el historial (una marca global): publicación completa.
        async with fabrica() as s:
            await s.execute(
                update(ProductCostState)
                .where(ProductCostState.product_id == grande)
                .values(pendiente_desde=date.min, revision=ProductCostState.revision + 1)
            )
            await s.commit()
        t0 = time.perf_counter()
        await svc.recalcular(fabrica, tid, [grande], solo_pendientes=True)
        dur = time.perf_counter() - t0
        filas.append((
            f"2b. recálculo de 1 producto ({args.eventos_grande} eventos), todo el historial",
            f"{dur:.2f} s", "≤ 4 s", dur <= 4,
        ))

        normal, marca = ids[0], ids[1]
        # «marca»: una compra sin costo deja el último registro incompleto, así
        # que cada venta marca (el camino sin omisión).
        async with fabrica() as s:
            await stock_service.increment_stock(marca, tid, 10_000, None, None, s)
            await s.commit()
        await svc.recalcular(fabrica, tid, [marca], solo_pendientes=True)
        async with fabrica() as s:
            await s.execute(
                update(Product).where(Product.id == normal).values(stock_units=100_000)
            )
            await s.execute(
                insert(InventoryMovement).values(
                    id=uuid.uuid4(), tenant_id=tid, product_id=normal, movement_type="purchase",
                    qty=100_000, unit_cost=Decimal("100"), occurred_at=BASE,
                    source_type="purchase_import",
                )
            )
            await s.commit()
        await svc.recalcular(fabrica, tid, [normal])

        # Intercalado (sin listener / con listener) para que la deriva de la
        # base no se cuente como costo del listener.
        base_n: list[float] = []
        con: dict[str, tuple[list[float], list[float]]] = {
            "omitida": ([], []), "marca": ([], []),
        }
        for _ in range(args.ventas_pos):
            _listener(False)
            base_n.append(await _vender(fabrica, tid, normal))
            _listener(True)
            for etiqueta, pid in (("omitida", normal), ("marca", marca)):
                commit, hooks = await _vender_medido(fabrica, tid, pid)
                con[etiqueta][0].append(commit)
                con[etiqueta][1].append(hooks)
        topes_p95 = {"omitida": 10, "marca": 15}
        for etiqueta, (commits, hooks) in con.items():
            # La compuerta es el tiempo dentro del listener. La resta de
            # percentiles del commit queda como referencia: con un fsync de
            # ~27 ms de mediana, su cola es ruido de la máquina.
            p95, p99 = _pct(hooks, 0.95), _pct(hooks, 0.99)
            ref = _pct(commits, 0.95) - _pct(base_n, 0.95)
            filas.append((
                f"3. latencia agregada al commit del POS ({etiqueta})",
                f"p95 {p95:.1f} ms · p99 {p99:.1f} ms en el listener"
                f" (ref. commit p95 {ref:+.1f})",
                f"p95 ≤ {topes_p95[etiqueta]} · p99 ≤ 25",
                p95 <= topes_p95[etiqueta] and p99 <= 25,
            ))

        for etiqueta, pid in (("normal", normal), ("incompleto", marca)):
            # Los recálculos de fondo dejan el producto pendiente antes de cada
            # vuelta, así que buena parte de estas ventas marcan aunque sean
            # «normales»: es la contención que se quiere medir.
            # El POS sólo toca product_cost_state desde el listener: una espera
            # por el advisory lock o la fila del estado cae dentro de los hooks.
            fondo = _Recalculos(url, connect_args, tid, [pid])
            medidas = [
                await _vender_medido(fabrica, tid, pid) for _ in range(args.ventas_pos // 2)
            ]
            vueltas = fondo.parar()
            espera = _pct([h for _, h in medidas], 0.99) if vueltas >= 0 else float("inf")
            ref = _pct([c for c, _ in medidas], 0.99) - _pct(base_n, 0.99)
            filas.append((
                f"4. espera del POS con recálculos concurrentes ({etiqueta})",
                f"p99 {espera:.1f} ms en el listener (ref. commit p99 {ref:+.1f};"
                f" {vueltas} recálculos)",
                "p99 ≤ 50", espera <= 50,
            ))

        # El import (otro proceso, como otro request) marca 50 productos con
        # ventas sin movimiento; mientras tanto el POS vende UNO de esos
        # productos y recálculos corren en un tercer proceso. Una espera por la
        # fila del estado cae dentro del listener del POS.
        afectados = ids[2:52]
        fondo = _Recalculos(url, connect_args, tid, afectados[:5])
        ctx = multiprocessing.get_context("spawn")
        duracion, ok = ctx.Value("d", float("nan")), ctx.Value("i", 0)
        proceso = ctx.Process(
            target=_proceso_import,
            args=(url, connect_args, tid, afectados, args.ventas_import, duracion, ok),
        )
        proceso.start()
        medidas: list[tuple[float, float]] = []
        try:
            while proceso.is_alive() or len(medidas) < 100:
                medidas.append(await _vender_medido(fabrica, tid, afectados[0]))
            fallo = False
        except Exception as exc:  # noqa: BLE001
            print(f"   el POS falló durante el import: {exc}")
            fallo = True
        proceso.join(timeout=120)
        deadlock = fondo.parar() < 0 or fallo or proceso.exitcode != 0 or ok.value != 1
        espera = _pct([h for _, h in medidas], 0.99) if medidas else float("inf")
        filas.append((
            f"5. import de {args.ventas_import} ventas + POS + recálculos",
            f"commit del import {duracion.value:.1f} s · POS p99 {espera:.1f} ms en el"
            f" listener ({len(medidas)} ventas)",
            "sin deadlock · p99 ≤ 50", (not deadlock) and espera <= 50,
        ))
    finally:
        async with fabrica() as s:
            for modelo in (
                ProductCostHistory, ProductCostState, InventoryMovement, InventoryBalance,
                SaleEntry, Product,
            ):
                await s.execute(delete(modelo).where(modelo.tenant_id == tid))
            await s.execute(delete(Tenant).where(Tenant.tenant_id == tid))
            await s.commit()
        await engine.dispose()

    print("\n| Medición | Resultado | Presupuesto | |")
    print("|---|---|---|---|")
    for nombre, res, presupuesto, ok in filas:
        print(f"| {nombre} | {res} | {presupuesto} | {'OK' if ok else 'NO'} |")
    print(f"\nMediana del commit del POS sin listener: {statistics.median(base_n):.1f} ms")


if __name__ == "__main__":
    asyncio.run(main())
