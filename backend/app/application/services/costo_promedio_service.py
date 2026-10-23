"""Costo promedio ponderado materializado: cálculo, publicación y lectura.

El promedio es un DATO; el costo principal de cada venta sigue siendo el de
reposición (``costo_de_referencia``). Acá se proyecta la historia de cada
producto (``domain/costo_promedio``) y se guarda en ``product_cost_history``.

**Contrato de publicación (M·P2b).** ``product_cost_state.revision`` cuenta los
cambios: la sube cada marca (``persistence/costo_promedio_pendiente``) y cada
publicación.

1. ``calcular`` lee las revisiones y la historia de los productos. En Postgres lo
   hace en una sola foto (``REPEATABLE READ``): no puede ver el stock antes de un
   commit y los movimientos después.
2. ``publicar`` corre en una transacción corta. Toma un advisory lock por
   producto, asegura la fila del estado (``INSERT … ON CONFLICT DO NOTHING``,
   revisión 0: el mismo protocolo que la primera marca), escribe el historial y
   recién AL FINAL relee la fila ``FOR UPDATE`` (así el lock que puede esperar una
   venta que marca dura milisegundos). Un producto cuya revisión cambió desde el
   cálculo se DESCARTA (su savepoint se revierte), así un cálculo atrasado nunca
   pisa uno más nuevo. Los que no cambiaron se publican y su revisión sube.

``reconstruir`` hace las dos cosas en la sesión del llamador (backfill, tests).
``recalcular`` las hace en transacciones separadas (job, drenaje).

**Ventas (M·P3).** Cada publicación recalcula el costo promedio de las ventas
afectadas (``sales_entries.cost_avg_*``, un DATO al lado del costo principal)
en la MISMA transacción que el historial, antes del ``FOR UPDATE`` del estado:
la guarda de revisión cubre las dos cosas (``actualizar_ventas_en``).

**Lectura.** ``leer_costo_a`` lee el estado y el registro vigente en UNA sentencia:
con algo pendiente desde ese día devuelve «recalculando», nunca un dato viejo.

``tenant_id`` lo pasa el llamador (script o job, nunca hay JWT acá); cada
consulta filtra por él.
"""

from __future__ import annotations

import uuid
import zlib
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final, cast

from sqlalchemy import Table, and_, delete, func, insert, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.business_time import today_ar
from app.domain.costo_de_venta import MOTIVO_UNIDAD_DUDOSA, es_unidad_dudosa
from app.domain.costo_promedio import PRIORIDAD, Registro, proyectar, registros
from app.domain.costo_promedio_de_venta import (
    PuntoDelHistorial,
    costo_promedio_de_venta,
    punto_a,
)
from app.observability.logger import get_logger
from app.persistence.models.product_cost import ProductCostHistory, ProductCostState
from app.persistence.repositories.costo_de_referencia_repository import costos_de_referencia
from app.persistence.repositories.costo_promedio_repository import (
    HistoriaProducto,
    cargar_historias,
)

LOTE: Final = 500
#: Orígenes del costo de reposición contra los que tiene sentido medir la
#: brecha: un costo tipeado no es una compra.
ORIGENES_DE_REPOSICION: Final = frozenset({"compra", "stock_inicial"})
#: Espacio de claves del advisory lock por producto (forma de dos int4: no se
#: superpone con la clave bigint del lock por tenant).
_ESPACIO_LOCK: Final = 0x4D50  # "MP"
MOTIVO_RECALCULANDO: Final = "recalculando"
MAX_ERROR: Final = 300

logger = get_logger(__name__)


@dataclass
class Resumen:
    tenant_id: uuid.UUID
    productos: int = 0
    con_registros: int = 0
    registros: int = 0
    publicados: int = 0
    #: Productos cuya revisión cambió entre el cálculo y la publicación: quedan
    #: pendientes para el próximo recálculo.
    descartados: int = 0
    #: Estado del último registro de cada producto con historial.
    por_estado: Counter[str] = field(default_factory=Counter)
    aproximados: int = 0
    fuera_del_ledger: int = 0
    #: ``1 − promedio / reposición`` de los productos ``medido`` (no aproximados)
    #: con reposición por compra. Positiva = el promedio está por debajo de lo que
    #: cuesta reponer.
    brechas: list[Decimal] = field(default_factory=list)
    #: Ventas cuyo costo promedio se recalculó (M·P3).
    ventas: int = 0


@dataclass
class Calculo:
    tenant_id: uuid.UUID
    #: Revisión leída por producto (0 = todavía no tenía fila de estado).
    revisiones: dict[uuid.UUID, int]
    historias: dict[uuid.UUID, HistoriaProducto]
    registros: dict[uuid.UUID, list[Registro]]
    #: Día desde el que se reemplazan los registros de cada producto; ``None`` =
    #: todo el historial (primera publicación o una marca de ``date.min``). Los
    #: registros anteriores no cambian: dependen sólo de los eventos anteriores.
    desde: dict[uuid.UUID, date | None] = field(default_factory=dict)
    #: Ventas cuyo costo promedio escribió la última publicación (M·P3).
    ventas: int = 0


@dataclass(frozen=True)
class Lectura:
    registro: ProductCostHistory | None
    #: ``recalculando`` si hay un cambio pendiente que alcanza al día pedido.
    motivo: str | None = None


def _tabla_estado() -> Table:
    return cast("Table", ProductCostState.__table__)


def _clave_lock(product_id: uuid.UUID) -> int:
    return zlib.crc32(product_id.bytes) - 2**31


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


async def calcular(
    session: AsyncSession, tenant_id: uuid.UUID, product_ids: Iterable[uuid.UUID] | None = None
) -> Calculo:
    """Lee revisiones e historia y proyecta. No escribe nada."""
    filtro = None if product_ids is None else list(dict.fromkeys(product_ids))
    consulta = select(
        ProductCostState.product_id,
        ProductCostState.revision,
        ProductCostState.pendiente_desde,
        ProductCostState.reconstruido_en,
    ).where(ProductCostState.tenant_id == tenant_id)
    if filtro is not None:
        consulta = consulta.where(ProductCostState.product_id.in_(filtro))
    revisiones: dict[uuid.UUID, int] = {}
    desde: dict[uuid.UUID, date | None] = {}
    for pid, revision, pendiente, reconstruido in (await session.execute(consulta)).tuples():
        revisiones[pid] = revision
        parcial = pendiente is not None and pendiente != date.min and reconstruido is not None
        desde[pid] = pendiente if parcial else None
    historias = await cargar_historias(session, tenant_id, filtro)
    regs: dict[uuid.UUID, list[Registro]] = {}
    for pid, h in historias.items():
        proy = proyectar(
            h.eventos, stock_fuera_del_ledger=h.stock_fuera_del_ledger, aproximado=h.aproximado
        )
        regs[pid] = registros(proy)
    return Calculo(
        tenant_id=tenant_id,
        revisiones={pid: revisiones.get(pid, 0) for pid in historias},
        historias=historias,
        registros=regs,
        desde={pid: desde.get(pid) for pid in historias},
    )


async def _asegurar_estados(
    session: AsyncSession, tenant_id: uuid.UUID, product_ids: Sequence[uuid.UUID]
) -> None:
    """La fila del estado existe (revisión 0) sin pisar una que creó una marca."""
    if not product_ids:
        return
    t = _tabla_estado()
    filas = [
        {
            "id": uuid.uuid4(), "tenant_id": tenant_id, "product_id": pid, "revision": 0,
            "intentos": 0, "stock_fuera_del_ledger": False, "aproximado": False,
        }
        for pid in product_ids
    ]
    insertar = pg_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert
    await session.execute(
        insertar(t).values(filas).on_conflict_do_nothing(
            index_elements=[t.c.tenant_id, t.c.product_id]
        )
    )


_COLUMNAS_COPY: Final = (
    "id", "tenant_id", "product_id", "dia", "clase_orden", "fecha", "creado", "evento_id",
    "clase", "u_conocidas", "u_sin_costo", "costo_promedio", "estado", "stock_conocido",
    "aproximado", "motivo", "revision",
)


async def _insertar_registros(session: AsyncSession, filas: list[dict[str, Any]]) -> None:
    """En Postgres, ``COPY`` (una publicación completa de un producto grande son
    decenas de miles de filas); en otros motores, ``INSERT`` por tandas."""
    if not filas:
        return
    if session.get_bind().dialect.name == "postgresql" and len(filas) > 1000:
        conexion = await session.connection()
        crudo = await conexion.get_raw_connection()
        await crudo.driver_connection.copy_records_to_table(  # type: ignore[union-attr]
            "product_cost_history",
            records=[tuple(f[c] for c in _COLUMNAS_COPY) for f in filas],
            columns=list(_COLUMNAS_COPY),
        )
        return
    for i in range(0, len(filas), 1000):
        await session.execute(insert(ProductCostHistory), filas[i : i + 1000])


class _CambioDuranteLaPublicacionError(Exception):
    def __init__(self, productos: set[uuid.UUID]) -> None:
        super().__init__(f"{len(productos)} productos cambiaron")
        self.productos = productos


async def _revisiones(
    session: AsyncSession, tenant_id: uuid.UUID, ids: Sequence[uuid.UUID], *, bloquear: bool
) -> dict[uuid.UUID, ProductCostState]:
    consulta = (
        select(ProductCostState)
        .where(ProductCostState.tenant_id == tenant_id, ProductCostState.product_id.in_(ids))
        .order_by(ProductCostState.product_id)
        .execution_options(populate_existing=True)
    )
    if bloquear:
        consulta = consulta.with_for_update()
    return {st.product_id: st for st in (await session.execute(consulta)).scalars()}


async def _escribir(
    session: AsyncSession, calculo: Calculo, ids: set[uuid.UUID]
) -> None:
    """Reemplaza el historial de ``ids`` y deja el estado publicado.

    El historial se escribe ANTES de bloquear la fila del estado; recién al
    final se toma ``FOR UPDATE`` y se compara la revisión. Así el lock de la fila
    (el que espera una venta que marca) dura milisegundos aunque el producto
    tenga miles de registros. Si alguna revisión cambió, se levanta
    ``_CambioDuranteLaPublicacionError`` y el savepoint del llamador se revierte.
    """
    tenant_id = calculo.tenant_id
    reg = ProductCostHistory
    completos = [pid for pid in ids if calculo.desde.get(pid) is None]
    if completos:
        await session.execute(
            delete(reg).where(reg.tenant_id == tenant_id, reg.product_id.in_(completos))
        )
    filas: list[dict[str, Any]] = []
    for pid in ids:
        dia = calculo.desde.get(pid)
        revision = calculo.revisiones[pid] + 1
        regs = calculo.registros[pid]
        if dia is not None:
            await session.execute(
                delete(reg).where(
                    reg.tenant_id == tenant_id, reg.product_id == pid, reg.dia >= dia
                )
            )
            regs = [r for r in regs if r.evento.fecha.date() >= dia]
        filas.extend(_fila(tenant_id, pid, r, revision) for r in regs)
    await _insertar_registros(session, filas)
    # M·2d-2: las ventas que quedaron ``sin_costo`` y ya tienen fuente. Cubre las
    # escrituras Core (dedup, relectura, M·R), que el listener del costeo no ve
    # pero sí marcan el producto; idempotente. Antes del costo promedio de las
    # ventas, que así se calcula sobre el snapshot nuevo.
    # Un error acá no frena el promedio: el costo principal es otro dato.
    try:
        async with session.begin_nested():
            await session.run_sync(lambda s: _recostear(s, tenant_id, ids))
    except Exception:  # noqa: BLE001 — se registra; la publicación sigue
        logger.exception("costeo.fuente_tardia_fallo", tenant_id=str(tenant_id))
    # M·P3: las ventas en la MISMA transacción que el historial y ANTES del FOR
    # UPDATE del estado: si la revisión cambió, el savepoint revierte las dos
    # cosas y el producto sigue pendiente (nunca una venta vieja con el
    # historial nuevo, ni al revés), y el lock que espera el POS no se alarga.
    calculo.ventas = await actualizar_ventas_en(session, calculo, ids)
    estados = await _revisiones(session, tenant_id, sorted(ids, key=str), bloquear=True)
    cambiaron = {pid for pid in ids if estados[pid].revision != calculo.revisiones[pid]}
    if cambiaron:
        raise _CambioDuranteLaPublicacionError(cambiaron)
    ahora = datetime.now(UTC)
    for pid in ids:
        st, h = estados[pid], calculo.historias[pid]
        st.revision = calculo.revisiones[pid] + 1
        st.pendiente_desde = None
        st.marcado_en = None
        st.intentos = 0
        st.ultimo_error = None
        st.stock_fuera_del_ledger = h.stock_fuera_del_ledger
        st.aproximado = h.aproximado
        st.reconstruido_en = ahora
    await session.flush()


def _recostear(s: Any, tenant_id: uuid.UUID, ids: set[uuid.UUID]) -> int:
    # Tarde: el módulo del costeo registra listeners de ``Session`` y su orden
    # depende de quién lo importa primero (``models/__init__``).
    from app.persistence.costeo_de_ventas import (  # noqa: PLC0415
        primeras_fuentes,
        recostear_sin_costo,
    )

    return recostear_sin_costo(s, primeras_fuentes(s, tenant_id, ids))


async def publicar(
    session: AsyncSession, calculo: Calculo
) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
    """Publica lo calculado. Devuelve ``(publicados, descartados)``.

    Un producto cuya revisión cambió desde el cálculo se descarta (sigue
    pendiente) y el resto se publica igual: cada intento va en un SAVEPOINT, así
    que descartar no revierte nada de la transacción del llamador.
    """
    tenant_id = calculo.tenant_id
    ids = sorted(calculo.historias, key=str)
    if not ids:
        return set(), set()
    if session.get_bind().dialect.name == "postgresql":
        for pid in ids:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:a, :b)"),
                {"a": _ESPACIO_LOCK, "b": _clave_lock(pid)},
            )
    await _asegurar_estados(session, tenant_id, ids)
    # Filtro previo sin lock: lo que ya cambió ni se escribe.
    leidas = await _revisiones(session, tenant_id, ids, bloquear=False)
    candidatos = {pid for pid in ids if leidas[pid].revision == calculo.revisiones[pid]}
    descartados = set(ids) - candidatos
    while candidatos:
        try:
            async with session.begin_nested():
                await _escribir(session, calculo, candidatos)
            break
        except _CambioDuranteLaPublicacionError as exc:
            candidatos -= exc.productos
            descartados |= exc.productos
    return candidatos, descartados


async def _medir_brecha(
    session: AsyncSession, calculo: Calculo, publicados: set[uuid.UUID], resumen: Resumen
) -> None:
    medidos: dict[uuid.UUID, Decimal] = {}
    for pid in publicados:
        regs = calculo.registros[pid]
        if not regs:
            continue
        est = regs[-1].estado
        if est.estado == "medido" and not est.aproximado and est.promedio and est.promedio > 0:
            medidos[pid] = est.promedio
    if not medidos:
        return
    reposicion = await costos_de_referencia(session, calculo.tenant_id, medidos, today_ar())
    for pid, prom in medidos.items():
        ref = reposicion.get(pid)
        if (
            ref is not None
            and ref.origen in ORIGENES_DE_REPOSICION
            and ref.costo_unitario
            and ref.costo_unitario > 0
        ):
            resumen.brechas.append(1 - prom / ref.costo_unitario)


def _sumar(
    resumen: Resumen,
    calculo: Calculo,
    publicados: set[uuid.UUID],
    descartados: set[uuid.UUID],
) -> None:
    resumen.productos += len(calculo.historias)
    resumen.publicados += len(publicados)
    resumen.descartados += len(descartados)
    for pid in publicados:
        h = calculo.historias[pid]
        regs = calculo.registros[pid]
        resumen.aproximados += h.aproximado
        resumen.fuera_del_ledger += h.stock_fuera_del_ledger
        resumen.registros += len(regs)
        if regs:
            resumen.con_registros += 1
            resumen.por_estado[regs[-1].estado.estado] += 1


async def reconstruir(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    product_ids: Iterable[uuid.UUID] | None = None,
    *,
    medir_brecha: bool = False,
) -> Resumen:
    """Calcula y publica en la sesión del llamador (backfill de un tenant, tests).
    No commitea. Con el mismo protocolo de revisión que ``recalcular``."""
    resumen = Resumen(tenant_id=tenant_id)
    calculo = await calcular(session, tenant_id, product_ids)
    publicados, descartados = await publicar(session, calculo)
    resumen.ventas += calculo.ventas
    _sumar(resumen, calculo, publicados, descartados)
    if medir_brecha:
        await _medir_brecha(session, calculo, publicados, resumen)
    return resumen


# ── M·P3: el costo promedio de las ventas ───────────────────────────────────

#: Ventas por sentencia al recalcular (Postgres: un ``UPDATE … FROM unnest``).
TANDA_VENTAS: Final = 5000
_SEIS: Final = Decimal("0.000001")


def _puntos_calculados(regs: list[Registro]) -> tuple[list[PuntoDelHistorial], list[date]]:
    """Los registros recién calculados como puntos, con el promedio llevado a la
    precisión con que se guarda (Numeric 18,6): así una venta da lo mismo
    costeada al confirmarse (lee lo guardado) que recalculada al publicar."""
    puntos = [
        PuntoDelHistorial(
            dia=r.evento.fecha.date(),
            promedio=(
                None
                if r.estado.promedio is None
                else r.estado.promedio.quantize(_SEIS, rounding=ROUND_HALF_UP)
            ),
            estado=r.estado.estado,
            aproximado=r.estado.aproximado,
            motivo=None if r.estado.motivo is None else r.estado.motivo[:200],
        )
        for r in regs
    ]
    return puntos, [p.dia for p in puntos]


async def _filas_de_ventas(
    session: AsyncSession, calculo: Calculo, pids: Sequence[uuid.UUID]
) -> list[dict[str, Any]]:
    """Las columnas ``cost_avg_*`` de las ventas vivas de ``pids`` afectadas por la
    publicación: día ≥ ``desde`` del producto, o todas si es el historial entero."""
    from app.persistence.models.product import Product  # noqa: PLC0415
    from app.persistence.models.transaction import SaleEntry  # noqa: PLC0415

    if not pids:
        return []
    tenant_id = calculo.tenant_id
    factores = dict(
        (
            await session.execute(
                select(Product.id, Product.base_units_per_sale_unit).where(
                    Product.tenant_id == tenant_id, Product.id.in_(list(pids))
                )
            )
        ).tuples().all()
    )
    desdes = [calculo.desde.get(pid) for pid in pids]
    consulta = select(
        SaleEntry.id, SaleEntry.product_id, SaleEntry.transaction_date, SaleEntry.quantity,
        SaleEntry.cost_reason,
    ).where(
        SaleEntry.tenant_id == tenant_id,
        SaleEntry.product_id.in_(list(pids)),
        SaleEntry.voided_at.is_(None),
    )
    if all(d is not None for d in desdes):
        minimo = min(d for d in desdes if d is not None)
        consulta = consulta.where(
            SaleEntry.transaction_date >= datetime.combine(minimo, datetime.min.time())
        )
    puntos = {pid: _puntos_calculados(calculo.registros.get(pid, [])) for pid in pids}
    filas: list[dict[str, Any]] = []
    for sid, pid, cuando, cantidad, motivo_costo in (await session.execute(consulta)).tuples():
        if pid is None:
            continue
        dia = cuando.date() if isinstance(cuando, datetime) else cuando
        desde = calculo.desde.get(pid)
        if desde is not None and dia < desde:
            continue
        lista, dias = puntos[pid]
        valores = costo_promedio_de_venta(
            punto_a(lista, dias, dia),
            unidades_base=cantidad,
            base_units_per_sale_unit=factores.get(pid) or 1,
            # La ruta que no confirma unidades base la marcó al costear (M·2a).
            unidad_dudosa=es_unidad_dudosa(motivo_costo),
            motivo_dudosa=MOTIVO_UNIDAD_DUDOSA,
        )
        filas.append({"id": sid, **valores.columnas(pendiente=False)})
    return filas


_UPDATE_VENTAS_PG: Final = text(
    "UPDATE sales_entries AS s SET "
    "cost_avg_unit_ars = v.u, cost_avg_ars = v.a, cost_avg_estado = v.e, "
    "cost_avg_aproximado = v.ap, cost_avg_motivo = v.m, cost_avg_pendiente = false "
    "FROM unnest(CAST(:ids AS uuid[]), CAST(:u AS numeric[]), CAST(:a AS numeric[]), "
    "CAST(:e AS varchar[]), CAST(:ap AS boolean[]), CAST(:m AS varchar[])) "
    "AS v(id, u, a, e, ap, m) "
    "WHERE s.id = v.id AND s.tenant_id = :tid"
)


async def _escribir_ventas(
    session: AsyncSession, tenant_id: uuid.UUID, filas: list[dict[str, Any]]
) -> None:
    """Una sentencia por tanda en Postgres; ``executemany`` en otros motores."""
    from app.persistence.models.transaction import SaleEntry  # noqa: PLC0415

    es_pg = session.get_bind().dialect.name == "postgresql"
    for i in range(0, len(filas), TANDA_VENTAS):
        tanda = filas[i : i + TANDA_VENTAS]
        if es_pg:
            await session.execute(
                _UPDATE_VENTAS_PG,
                {
                    "tid": tenant_id,
                    "ids": [f["id"] for f in tanda],
                    "u": [f["cost_avg_unit_ars"] for f in tanda],
                    "a": [f["cost_avg_ars"] for f in tanda],
                    "e": [f["cost_avg_estado"] for f in tanda],
                    "ap": [f["cost_avg_aproximado"] for f in tanda],
                    "m": [f["cost_avg_motivo"] for f in tanda],
                },
            )
        else:
            await session.execute(update(SaleEntry), tanda)


async def actualizar_ventas_en(
    session: AsyncSession, calculo: Calculo, pids: Iterable[uuid.UUID]
) -> int:
    """Recalcula el costo promedio de las ventas de ``pids`` en la sesión del
    llamador. Lo llama ``_escribir`` dentro de la publicación. Devuelve cuántas
    escribió."""
    filas = await _filas_de_ventas(session, calculo, sorted(pids, key=str))
    await _escribir_ventas(session, calculo.tenant_id, filas)
    return len(filas)


async def productos_pendientes(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    marcados_antes_de: datetime | None = None,
    limite: int | None = None,
    product_ids: Iterable[uuid.UUID] | None = None,
) -> list[uuid.UUID]:
    s = ProductCostState
    consulta = (
        select(s.product_id)
        .where(s.tenant_id == tenant_id, s.pendiente_desde.is_not(None))
        .order_by(s.marcado_en.asc().nulls_first(), s.product_id)
    )
    if marcados_antes_de is not None:
        consulta = consulta.where(or_(s.marcado_en.is_(None), s.marcado_en < marcados_antes_de))
    if product_ids is not None:
        consulta = consulta.where(s.product_id.in_(list(product_ids)))
    if limite is not None:
        consulta = consulta.limit(limite)
    return list((await session.execute(consulta)).scalars())


async def recalcular(
    fabrica: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    product_ids: Iterable[uuid.UUID] | None = None,
    *,
    solo_pendientes: bool = False,
    medir_brecha: bool = False,
    lote: int = LOTE,
) -> Resumen:
    """Recalcula por lotes, cada uno en dos transacciones: el cálculo (una foto
    consistente, sin locks) y la publicación (corta, con la revisión)."""
    resumen = Resumen(tenant_id=tenant_id)
    async with fabrica() as s:
        if solo_pendientes:
            filtro = None if product_ids is None else list(dict.fromkeys(product_ids))
            ids = await productos_pendientes(s, tenant_id, product_ids=filtro)
        elif product_ids is not None:
            ids = list(dict.fromkeys(product_ids))
        else:
            ids = list(await _productos_del_tenant(s, tenant_id))
    for i in range(0, len(ids), lote):
        tramo = ids[i : i + lote]
        async with fabrica() as s:
            if s.get_bind().dialect.name == "postgresql":
                await s.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            calculo = await calcular(s, tenant_id, tramo)
            await s.rollback()
        async with fabrica() as s:
            publicados, descartados = await publicar(s, calculo)
            if medir_brecha:
                await _medir_brecha(s, calculo, publicados, resumen)
            await s.commit()
        resumen.ventas += calculo.ventas
        _sumar(resumen, calculo, publicados, descartados)
    return resumen


async def _productos_del_tenant(session: AsyncSession, tenant_id: uuid.UUID) -> Sequence[uuid.UUID]:
    from app.persistence.models.product import Product  # noqa: PLC0415

    return (
        await session.execute(select(Product.id).where(Product.tenant_id == tenant_id))
    ).scalars().all()


async def registrar_fallo(
    fabrica: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    product_id: uuid.UUID,
    error: str,
) -> None:
    """Deja el fallo a la vista; la marca sigue pendiente."""
    async with fabrica() as s:
        st = (
            await s.execute(
                select(ProductCostState).where(
                    ProductCostState.tenant_id == tenant_id,
                    ProductCostState.product_id == product_id,
                )
            )
        ).scalar_one_or_none()
        if st is not None:
            st.intentos = (st.intentos or 0) + 1
            st.ultimo_error = error[:MAX_ERROR]
            await s.commit()


async def leer_costo_a(
    session: AsyncSession, tenant_id: uuid.UUID, product_id: uuid.UUID, dia: date
) -> Lectura:
    """El registro vigente al cierre de ``dia``, leído en una sola sentencia junto
    con el estado. Con un cambio pendiente que alcanza a ``dia`` (o al historial
    entero), ``None`` con el motivo «recalculando»."""
    h = ProductCostHistory
    s = ProductCostState
    ultimo = (
        select(h.id)
        .where(h.tenant_id == tenant_id, h.product_id == product_id, h.dia <= dia)
        .order_by(
            h.dia.desc(), h.clase_orden.desc(), h.fecha.desc(), h.creado.desc(),
            h.evento_id.desc(),
        )
        .limit(1)
        .scalar_subquery()
    )
    fila = (
        await session.execute(
            select(s.pendiente_desde, h)
            .select_from(s)
            .outerjoin(h, and_(h.id == ultimo))
            .where(s.tenant_id == tenant_id, s.product_id == product_id)
        )
    ).first()
    if fila is None:
        return Lectura(None)
    pendiente, registro = fila
    if pendiente is not None and pendiente <= dia:
        return Lectura(None, MOTIVO_RECALCULANDO)
    return Lectura(registro)


async def costo_a(
    session: AsyncSession, tenant_id: uuid.UUID, product_id: uuid.UUID, dia: date
) -> ProductCostHistory | None:
    """Lectura CRUDA del registro vigente (sin mirar lo pendiente)."""
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


async def costos_a_lote(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    product_ids: Iterable[uuid.UUID],
    dia: date,
) -> dict[uuid.UUID, ProductCostHistory]:
    """``costo_a`` para varios productos en UNA sentencia (M·P4, la pantalla de
    productos). Lectura CRUDA, igual que ``costo_a``: con un cambio pendiente
    devuelve el último registro publicado, nunca «recalculando» (decisión del
    dueño 2026-10-14). Un producto sin registro a ``dia`` no aparece.

    Top-1 correlacionado por producto (el índice ``(tenant, producto, día)``
    corta en el primero): un ``row_number()`` sobre la página leía y ordenaba
    TODO el historial de cada producto (737 ms contra 4,6 ms con 200 productos
    de 2.500 registros, revisión de M·P4-1)."""
    from app.persistence.models.product import Product  # noqa: PLC0415

    ids = list(dict.fromkeys(product_ids))
    if not ids:
        return {}
    h = ProductCostHistory
    ultimo = (
        select(h.id)
        .where(h.tenant_id == tenant_id, h.product_id == Product.id, h.dia <= dia)
        .order_by(
            h.dia.desc(), h.clase_orden.desc(), h.fecha.desc(), h.creado.desc(),
            h.evento_id.desc(),
        )
        .limit(1)
        .correlate(Product)
        .scalar_subquery()
    )
    filas = (
        await session.execute(
            select(h)
            .join(Product, h.id == ultimo)
            .where(Product.tenant_id == tenant_id, Product.id.in_(ids))
        )
    ).scalars().all()
    return {fila.product_id: fila for fila in filas}


async def estadisticas_pendientes(session: AsyncSession) -> dict[str, Any]:
    """Para el seguimiento: cuántos pendientes hay, el más viejo y los que fallan."""
    s = ProductCostState
    total, mas_viejo = (
        await session.execute(
            select(func.count(), func.min(s.marcado_en)).where(s.pendiente_desde.is_not(None))
        )
    ).one()
    con_errores = await session.scalar(
        select(func.count()).where(s.pendiente_desde.is_not(None), s.intentos > 3)
    )
    antiguedad = None
    if mas_viejo is not None:
        viejo = mas_viejo if mas_viejo.tzinfo else mas_viejo.replace(tzinfo=UTC)
        antiguedad = int((datetime.now(UTC) - viejo) / timedelta(seconds=1))
    return {
        "pendientes": int(total),
        "antiguedad_s": antiguedad,
        "con_errores": int(con_errores or 0),
    }


def _seis(v: Decimal | None) -> Decimal | None:
    return None if v is None else v.quantize(Decimal("0.000001"))


def _firma_publicada(r: ProductCostHistory) -> tuple[Any, ...]:
    return (
        r.dia, r.clase, r.evento_id,
        _seis(None if r.costo_promedio is None else Decimal(r.costo_promedio)),
        r.estado, r.stock_conocido, r.aproximado,
    )


def _firma_calculada(r: Registro) -> tuple[Any, ...]:
    est = r.estado
    return (
        r.evento.fecha.date(), r.evento.clase, r.evento.id,
        _seis(est.promedio),
        est.estado, est.stock_conocido, est.aproximado,
    )


async def verificar(session: AsyncSession, tenant_id: uuid.UUID) -> list[uuid.UUID]:
    """Productos cuyo historial guardado NO coincide con una proyección fresca y
    que no están marcados como pendientes. No escribe nada."""
    calculo = await calcular(session, tenant_id)
    pendientes = set(await productos_pendientes(session, tenant_id))
    guardado: dict[uuid.UUID, list[tuple[Any, ...]]] = {}
    h = ProductCostHistory
    for r in (
        await session.execute(
            select(h)
            .where(h.tenant_id == tenant_id)
            .order_by(h.product_id, h.dia, h.clase_orden, h.fecha, h.creado, h.evento_id)
        )
    ).scalars():
        guardado.setdefault(r.product_id, []).append(_firma_publicada(r))
    distintos = []
    for pid, regs in calculo.registros.items():
        if pid in pendientes:
            continue
        if [_firma_calculada(r) for r in regs] != guardado.get(pid, []):
            distintos.append(pid)
    return distintos
