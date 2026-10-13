"""Costo promedio ponderado materializado (M·P2a): reconstrucción y lectura.

El promedio es un DATO; el costo principal de cada venta sigue siendo el de
reposición (``costo_de_referencia``). Acá se proyecta la historia de cada
producto (``domain/costo_promedio``) y se guarda un registro por compra,
apertura o ancla en ``product_cost_history``. Todavía no lo lee nadie: M·P2a es
el modo sombra. Los disparadores, el job en segundo plano y la reconstrucción
desde una fecha son M·P2b.

``reconstruir`` reemplaza el historial de los productos pedidos y sube su
``revision`` en ``product_cost_state``. Es idempotente: reconstruir dos veces da
los mismos registros. ``tenant_id`` lo pasa el llamador (script o job, nunca
hay JWT acá); cada consulta filtra por él.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.business_time import today_ar
from app.domain.costo_promedio import PRIORIDAD, Registro, proyectar, registros
from app.persistence.models.product_cost import ProductCostHistory, ProductCostState
from app.persistence.repositories.costo_de_referencia_repository import costos_de_referencia
from app.persistence.repositories.costo_promedio_repository import cargar_historias

LOTE: Final = 1000
#: Orígenes del costo de reposición contra los que tiene sentido medir la
#: brecha: un costo tipeado no es una compra.
ORIGENES_DE_REPOSICION: Final = frozenset({"compra", "stock_inicial"})


@dataclass
class Resumen:
    tenant_id: uuid.UUID
    productos: int = 0
    con_registros: int = 0
    registros: int = 0
    #: Estado del último registro de cada producto con historial.
    por_estado: Counter[str] = field(default_factory=Counter)
    aproximados: int = 0
    fuera_del_ledger: int = 0
    #: ``1 − promedio / reposición`` de los productos ``medido`` (no aproximados)
    #: con reposición por compra. Positiva = el promedio está por debajo de lo que
    #: cuesta reponer.
    brechas: list[Decimal] = field(default_factory=list)


def _fila(
    tenant_id: uuid.UUID, product_id: uuid.UUID, r: Registro, revision: int
) -> dict[str, Any]:
    ev, est = r.evento, r.estado
    return {
        "id": uuid.uuid4(),
        "tenant_id": tenant_id,
        "product_id": product_id,
        "dia": ev.fecha.date(),
        "clase_orden": PRIORIDAD[ev.clase],
        "fecha": ev.fecha,
        "creado": ev.creado,
        "evento_id": ev.id,
        "clase": ev.clase,
        "u_conocidas": est.u_conocidas,
        "u_sin_costo": est.u_sin_costo,
        "costo_promedio": est.promedio,
        "estado": est.estado,
        "stock_conocido": est.stock_conocido,
        "aproximado": est.aproximado,
        "motivo": None if est.motivo is None else est.motivo[:200],
        "revision": revision,
    }


async def reconstruir(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    product_ids: Iterable[uuid.UUID] | None = None,
    *,
    medir_brecha: bool = False,
) -> Resumen:
    """Proyecta de nuevo la historia completa de los productos y reemplaza su
    historial. ``medir_brecha`` agrega la comparación con la reposición (una
    consulta más; la usa el informe de sombra)."""
    historias = await cargar_historias(session, tenant_id, product_ids)
    resumen = Resumen(tenant_id=tenant_id, productos=len(historias))
    ids = list(historias)
    previos: dict[uuid.UUID, ProductCostState] = {}
    for i in range(0, len(ids), LOTE):
        lote = ids[i : i + LOTE]
        for st in (
            await session.execute(
                select(ProductCostState)
                .where(
                    ProductCostState.tenant_id == tenant_id,
                    ProductCostState.product_id.in_(lote),
                )
                .with_for_update()
            )
        ).scalars():
            previos[st.product_id] = st
        await session.execute(
            delete(ProductCostHistory).where(
                ProductCostHistory.tenant_id == tenant_id,
                ProductCostHistory.product_id.in_(lote),
            )
        )

    filas: list[dict[str, Any]] = []
    ultimos: dict[uuid.UUID, Registro] = {}
    ahora = datetime.now(UTC)
    for pid, h in historias.items():
        proy = proyectar(
            h.eventos, stock_fuera_del_ledger=h.stock_fuera_del_ledger, aproximado=h.aproximado
        )
        regs = registros(proy)
        previo = previos.get(pid)
        revision = (previo.revision if previo is not None else 0) + 1
        filas.extend(_fila(tenant_id, pid, r, revision) for r in regs)
        if previo is None:
            session.add(
                ProductCostState(
                    tenant_id=tenant_id,
                    product_id=pid,
                    revision=revision,
                    pendiente_desde=None,
                    stock_fuera_del_ledger=h.stock_fuera_del_ledger,
                    aproximado=h.aproximado,
                    reconstruido_en=ahora,
                )
            )
        else:
            previo.revision = revision
            previo.pendiente_desde = None
            previo.stock_fuera_del_ledger = h.stock_fuera_del_ledger
            previo.aproximado = h.aproximado
            previo.reconstruido_en = ahora
        resumen.aproximados += h.aproximado
        resumen.fuera_del_ledger += h.stock_fuera_del_ledger
        if regs:
            resumen.con_registros += 1
            ultimos[pid] = regs[-1]
            resumen.por_estado[regs[-1].estado.estado] += 1
    resumen.registros = len(filas)
    for i in range(0, len(filas), LOTE):
        await session.execute(insert(ProductCostHistory), filas[i : i + LOTE])
    await session.flush()

    if medir_brecha:
        medidos = {
            pid: r.estado.promedio
            for pid, r in ultimos.items()
            if r.estado.estado == "medido"
            and not r.estado.aproximado
            and r.estado.promedio
            and r.estado.promedio > 0
        }
        if medidos:
            reposicion = await costos_de_referencia(session, tenant_id, medidos, today_ar())
            for pid, prom in medidos.items():
                ref = reposicion.get(pid)
                if (
                    ref is not None
                    and ref.origen in ORIGENES_DE_REPOSICION
                    and ref.costo_unitario
                    and ref.costo_unitario > 0
                ):
                    resumen.brechas.append(1 - prom / ref.costo_unitario)
    return resumen


async def costo_a(
    session: AsyncSession, tenant_id: uuid.UUID, product_id: uuid.UUID, dia: date
) -> ProductCostHistory | None:
    """El registro vigente al cierre de ``dia``: el último de ese día o anterior,
    en el orden del dominio. ``None`` = el producto no tenía compras todavía."""
    h = ProductCostHistory
    return (
        await session.execute(
            select(h)
            .where(h.tenant_id == tenant_id, h.product_id == product_id, h.dia <= dia)
            .order_by(
                h.dia.desc(), h.clase_orden.desc(), h.fecha.desc(), h.creado.desc(),
                h.evento_id.desc(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
