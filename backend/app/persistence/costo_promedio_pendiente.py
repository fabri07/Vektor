"""Qué productos tienen el costo promedio pendiente de recalcular (M·P2b).

El historial del costo promedio (``product_cost_history``) se deriva del ledger.
Este listener anota, en cada transacción, qué productos lo dejan viejo, y al
confirmar los marca en ``product_cost_state`` (``pendiente_desde``, ``revision``)
y encola el recálculo. El promedio es un DATO: nada de esto toca el costo
principal (la reposición).

**Qué se publica.** Por producto: sus registros (compras, aperturas, anclas y todo
evento que cambie estado, promedio, ``stock_conocido``, ``aproximado`` o motivo)
y las marcas globales ``aproximado`` / ``stock_fuera_del_ledger``, que se copian
a TODO el historial.

**Lo único que se puede omitir** es una salida NUEVA y NORMAL —un ``sale`` con
``sale:{id}`` de una venta creada en la misma transacción, o una merma— cuando,
además, el producto no tiene registros con día posterior, su último registro no
está ``incompleto`` y el stock queda > 0. Queda al final de la línea de tiempo,
no cambia el estado y no cambia las marcas (sin clamp, la venta tiene su
movimiento y el ledger sigue reproduciendo ``stock_units``). La próxima compra
reconstruye el producto entero y la cuenta. Es la venta del POS de todos los días:
no escribe estado ni toma locks.

**Todo lo demás marca:** entradas, anulaciones, reactivaciones, borrados, cambios
de cantidad/signo/clase/producto/fecha/origen, ventas sin su movimiento (mueven
``aproximado``) y cambios de B11. Lo que mueve una marca global invalida TODO el
historial (``pendiente_desde = date.min``). Si no se conoce un valor anterior, se
invalida todo el historial: marcar de más cuesta un recálculo; marcar de menos
deja un dato viejo que parece válido.

**Savepoints.** Un rollback de nivel superior descarta lo anotado; el de un
savepoint no descarta nada (marcaría de más, nunca de menos). Se marca y se
encola sólo en el commit de nivel superior.
"""

from __future__ import annotations

import contextlib
import threading
import uuid
from collections.abc import Iterable
from datetime import UTC, date, datetime
from typing import Any, Final, cast

from sqlalchemy import (
    Date,
    Table,
    and_,
    case,
    event,
    exists,
    func,
    inspect,
    literal,
    or_,
    select,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import InstanceState, Session

from app.domain.business_time import a_hora_de_negocio
from app.observability.logger import get_logger
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.pos_stock_discrepancy import PosStockDiscrepancy
from app.persistence.models.product import Product
from app.persistence.models.product_cost import ProductCostHistory, ProductCostState
from app.persistence.models.transaction import SaleEntry

logger = get_logger(__name__)

TODO_EL_HISTORIAL: Final = date.min
PREFIJO_VENTA: Final = "sale:"

_MARCAS: Final = "mp2b_marcas"  # dict[(tenant, producto)] -> día
_CANDIDATAS: Final = "mp2b_salidas_omitibles"  # list[(tenant, producto, día, venta|None)]
_VENTAS_NUEVAS: Final = "mp2b_ventas_nuevas"  # dict[venta] -> (tenant, producto)
_VENTAS_CON_MOV: Final = "mp2b_ventas_con_movimiento"  # set[venta]
_A_ENCOLAR: Final = "mp2b_a_encolar"  # list[(tenant, producto)]
_STOCK_TOCADO: Final = "mp2b_stock_tocado"  # set[(tenant, producto)] con stock_units cambiado
_CON_MOVIMIENTO: Final = "mp2b_con_movimiento"  # set[(tenant, producto)] con movimiento tocado
_CLAVES: Final = (
    _MARCAS, _CANDIDATAS, _VENTAS_NUEVAS, _VENTAS_CON_MOV, _A_ENCOLAR, _STOCK_TOCADO,
    _CON_MOVIMIENTO,
)
#: Encolar en un hilo aparte (los tests lo apagan para ver el encolado en línea).
ENCOLAR_EN_HILO: bool = True
#: Productos por tarea: un import que marca miles no publica miles de mensajes.
TANDA_DE_ENCOLADO: Final = 200
#: Filas por upsert de marcas (9 parámetros por fila, lejos del tope de Postgres).
TANDA_DE_MARCAS: Final = 500
#: Productos por consulta de omisión (tres parámetros por producto).
TANDA_DE_DECISION: Final = 2000

_COLUMNAS_DEL_MOVIMIENTO: Final = (
    "qty", "voided_at", "occurred_at", "product_id", "unit_cost", "unit_cost_final",
    "movement_type", "source_type", "source_event_id",
)
_COLUMNAS_DE_LA_VENTA: Final = ("voided_at", "product_id", "quantity", "source_upload_id")


# ── anotar ──────────────────────────────────────────────────────────────────


def _marcar(session: Session, tenant_id: Any, product_id: Any, desde: date) -> None:
    if tenant_id is None or product_id is None:
        return
    marcas: dict[tuple[uuid.UUID, uuid.UUID], date] = session.info.setdefault(_MARCAS, {})
    # Normalizado: un id como str y el mismo como UUID serían dos filas en la
    # misma tanda del upsert («cannot affect row a second time»).
    clave = (_uuid(tenant_id), _uuid(product_id))
    actual = marcas.get(clave)
    marcas[clave] = desde if actual is None else min(actual, desde)


def marcas_en_curso(session: Session) -> dict[tuple[uuid.UUID, uuid.UUID], date]:
    """Las marcas de la transacción actual que todavía no se escribieron.

    Se escriben en ``before_commit`` de este módulo, que corre DESPUÉS del costeo
    de ventas (orden de registro en ``models/__init__``; el costeo importa este
    módulo tarde, dentro de la función, para no cambiarlo): el costeo las
    consulta acá para saber que el producto se va a recalcular (M·P3).
    """
    return dict(session.info.get(_MARCAS, {}))


def _uuid(valor: Any) -> uuid.UUID:
    return valor if isinstance(valor, uuid.UUID) else uuid.UUID(str(valor))


def _estado_orm(obj: object) -> InstanceState[Any]:
    return cast("InstanceState[Any]", inspect(obj))


def marcar_todo_el_historial(
    session: Any, tenant_id: uuid.UUID, product_ids: Iterable[uuid.UUID | None]
) -> None:
    """Para las escrituras Core de la app (``update()``/``delete()`` sobre el
    ledger o las ventas) que el listener no ve: dejan TODO el historial de esos
    productos pendiente, y se marca al confirmar como cualquier otra marca.
    Acepta una ``Session`` o una ``AsyncSession``."""
    sync = getattr(session, "sync_session", session)
    for pid in product_ids:
        _marcar(sync, tenant_id, pid, TODO_EL_HISTORIAL)


def _cargado(obj: object, columna: str) -> bool:
    return columna not in _estado_orm(obj).unloaded


def _dia_del_movimiento(occurred_at: datetime | None, created_at: datetime | None) -> date:
    """El día de negocio de un movimiento; ``date.min`` si no se sabe."""
    if occurred_at is not None:
        return occurred_at.date()
    if created_at is not None:
        instante = created_at if created_at.tzinfo is not None else created_at.replace(tzinfo=UTC)
        return a_hora_de_negocio(instante).date()
    return TODO_EL_HISTORIAL


def _valor_anterior(obj: object, columna: str) -> tuple[bool, Any]:
    """``(se conoce, valor)`` del valor de ``columna`` antes del flush."""
    hist = _estado_orm(obj).attrs[columna].history
    if hist.deleted:
        return True, hist.deleted[0]
    if hist.unchanged:
        return True, hist.unchanged[0]
    return False, None


def _con_movimiento(session: Session, tenant_id: Any, product_id: Any) -> None:
    session.info.setdefault(_CON_MOVIMIENTO, set()).add((tenant_id, product_id))


def _movimiento_nuevo(session: Session, m: InventoryMovement) -> None:
    _con_movimiento(session, m.tenant_id, m.product_id)
    occurred = m.occurred_at if _cargado(m, "occurred_at") else None
    dia = _dia_del_movimiento(occurred, None)
    if dia == TODO_EL_HISTORIAL:
        # Recién creado sin fecha de negocio: es de ahora.
        from app.domain.business_time import today_ar  # noqa: PLC0415

        dia = today_ar()
    evento = m.source_event_id or ""
    es_venta = m.movement_type == "sale" and evento.startswith(PREFIJO_VENTA)
    if evento.startswith(PREFIJO_VENTA):
        venta = evento[len(PREFIJO_VENTA):]
        with contextlib.suppress(ValueError):
            session.info.setdefault(_VENTAS_CON_MOV, set()).add(uuid.UUID(venta))
    normal = (
        m.voided_at is None
        and (m.qty or 0) < 0
        and (es_venta or m.movement_type == "loss")
    )
    if normal:
        venta_id: uuid.UUID | None = None
        if es_venta:
            try:
                venta_id = uuid.UUID(evento[len(PREFIJO_VENTA):])
            except ValueError:
                normal = False
        if normal:
            session.info.setdefault(_CANDIDATAS, []).append(
                (m.tenant_id, m.product_id, dia, venta_id)
            )
            return
    _marcar(session, m.tenant_id, m.product_id, dia)


def _movimiento_modificado(session: Session, m: InventoryMovement) -> None:
    estado = _estado_orm(m)
    if not any(estado.attrs[c].history.has_changes() for c in _COLUMNAS_DEL_MOVIMIENTO):
        return
    _con_movimiento(session, m.tenant_id, m.product_id)
    conoce_prod, prod_viejo = _valor_anterior(m, "product_id")
    conoce_fecha, fecha_vieja = _valor_anterior(m, "occurred_at")
    creado = m.created_at if _cargado(m, "created_at") else None
    dia_nuevo = _dia_del_movimiento(m.occurred_at, creado)
    dia_viejo = _dia_del_movimiento(fecha_vieja, creado) if conoce_fecha else TODO_EL_HISTORIAL
    desde = min(dia_nuevo, dia_viejo)
    _marcar(session, m.tenant_id, m.product_id, desde)
    if not conoce_prod:
        # No se sabe de qué producto venía: lo único seguro es este, entero.
        _marcar(session, m.tenant_id, m.product_id, TODO_EL_HISTORIAL)
        logger.warning("costo_promedio.producto_anterior_desconocido", movement_id=str(m.id))
    elif prod_viejo != m.product_id:
        _marcar(session, m.tenant_id, prod_viejo, desde)


def _venta_tocada(session: Session, v: SaleEntry, *, nueva: bool) -> None:
    if nueva:
        if v.product_id is not None and v.id is not None:
            session.info.setdefault(_VENTAS_NUEVAS, {})[v.id] = (v.tenant_id, v.product_id)
        return
    estado = _estado_orm(v)
    if not any(estado.attrs[c].history.has_changes() for c in _COLUMNAS_DE_LA_VENTA):
        return
    # Mueve ``aproximado``, que está copiado en todo el historial.
    _marcar(session, v.tenant_id, v.product_id, TODO_EL_HISTORIAL)
    conoce, prod_viejo = _valor_anterior(v, "product_id")
    if conoce and prod_viejo != v.product_id:
        _marcar(session, v.tenant_id, prod_viejo, TODO_EL_HISTORIAL)


@event.listens_for(Session, "after_flush")
def _anotar(session: Session, _flush_context: Any) -> None:
    for obj in session.new:
        if isinstance(obj, InventoryMovement):
            _movimiento_nuevo(session, obj)
        elif isinstance(obj, SaleEntry):
            _venta_tocada(session, obj, nueva=True)
        elif isinstance(obj, PosStockDiscrepancy):
            _marcar(session, obj.tenant_id, obj.product_id, TODO_EL_HISTORIAL)
    for obj in session.dirty:
        if (
            isinstance(obj, Product)
            and _estado_orm(obj).attrs["stock_units"].history.has_changes()
        ):
            session.info.setdefault(_STOCK_TOCADO, set()).add((obj.tenant_id, obj.id))
        elif isinstance(obj, InventoryMovement):
            _movimiento_modificado(session, obj)
        elif isinstance(obj, SaleEntry):
            _venta_tocada(session, obj, nueva=False)
        elif (
            isinstance(obj, PosStockDiscrepancy)
            and _estado_orm(obj).attrs["status"].history.has_changes()
        ):
            _marcar(session, obj.tenant_id, obj.product_id, TODO_EL_HISTORIAL)
    for obj in session.deleted:
        if isinstance(obj, InventoryMovement | SaleEntry | PosStockDiscrepancy):
            _marcar(session, obj.tenant_id, obj.product_id, TODO_EL_HISTORIAL)


@event.listens_for(Session, "after_soft_rollback")
def _olvidar(session: Session, previous_transaction: Any) -> None:
    # Sólo el rollback de nivel superior: el de un savepoint no descarta nada.
    if previous_transaction.parent is None:
        for clave in _CLAVES:
            session.info.pop(clave, None)


# ── confirmar ───────────────────────────────────────────────────────────────


def _no_omitibles(tenant_id: uuid.UUID, items: dict[uuid.UUID, date]) -> Any:
    """La condición de las salidas candidatas que NO se pueden omitir.

    Se marca si hay un registro con día posterior, el último registro
    ``incompleto``, el stock en 0 (pudo haber clamp) o el estado ya pendiente
    (no se decide sobre un historial en revisión). **Sin nada publicado
    también se marca (M·P3):** la venta guarda su costo promedio y, sin
    historial, sólo una publicación se lo puede dar; omitirla la dejaba sin
    valor hasta el próximo evento del producto.
    El último registro sale de subconsultas ``ORDER BY … LIMIT 1`` sobre el
    índice ``(tenant_id, product_id, dia)``.
    """
    h = ProductCostHistory
    st = ProductCostState
    orden = (
        h.dia.desc(), h.clase_orden.desc(), h.fecha.desc(), h.creado.desc(),
        h.evento_id.desc(),
    )

    def _ultimo(col: Any) -> Any:
        return (
            select(col)
            .where(h.tenant_id == tenant_id, h.product_id == Product.id)
            .order_by(*orden)
            .limit(1)
            .scalar_subquery()
        )

    dia = case(*[(Product.id == pid, literal(d, Date())) for pid, d in items.items()])
    pendiente = exists().where(
        st.tenant_id == tenant_id, st.product_id == Product.id, st.pendiente_desde.is_not(None)
    )
    ultimo_dia = _ultimo(h.dia)
    condicion = and_(
        Product.tenant_id == tenant_id,
        Product.id.in_(list(items)),
        or_(
            # Pendiente aunque no haya nada publicado: puede haber un cálculo en
            # curso, y sin subir la revisión publicaría sin esta salida.
            pendiente,
            ultimo_dia.is_(None),
            and_(
                ultimo_dia.is_not(None),
                or_(
                    ultimo_dia > dia,
                    _ultimo(h.estado) == "incompleto",
                    func.coalesce(Product.stock_units, 0) <= 0,
                ),
            ),
        ),
    )
    return condicion


def _resolver_candidatas(session: Session) -> dict[uuid.UUID, dict[uuid.UUID, date]]:
    """Marca lo que ya se sabe que marca y devuelve, por tenant, las salidas
    candidatas a omisión que quedan por decidir (producto → día menor)."""
    candidatas: list[tuple[uuid.UUID, uuid.UUID, date, uuid.UUID | None]] = session.info.pop(
        _CANDIDATAS, []
    )
    nuevas: dict[uuid.UUID, tuple[uuid.UUID, uuid.UUID]] = session.info.pop(_VENTAS_NUEVAS, {})
    con_mov: set[uuid.UUID] = session.info.pop(_VENTAS_CON_MOV, set())
    # Venta nueva sin su movimiento en esta transacción: mueve ``aproximado``.
    for nueva_id, (tenant_id, product_id) in nuevas.items():
        if nueva_id not in con_mov:
            _marcar(session, tenant_id, product_id, TODO_EL_HISTORIAL)
    por_tenant: dict[uuid.UUID, dict[uuid.UUID, date]] = {}
    for tenant_id, product_id, dia, venta_id in candidatas:
        if tenant_id is None or product_id is None:
            continue
        if venta_id is not None and venta_id not in nuevas:
            # Descuenta una venta que no se creó acá (p. ej. B11 que se resuelve):
            # no es la salida normal del día.
            _marcar(session, tenant_id, product_id, dia)
            continue
        items = por_tenant.setdefault(_uuid(tenant_id), {})
        pid = _uuid(product_id)
        items[pid] = min(items.get(pid, dia), dia)
    return por_tenant


def _marcar_no_omitibles(
    session: Session, por_tenant: dict[uuid.UUID, dict[uuid.UUID, date]]
) -> None:
    """Decide con UN ``SELECT`` por tenant (el camino de cada venta del POS) y
    deja las marcas para el upsert ordenado de ``marcar_pendientes``.

    Probado (2026-10-13): decidir y marcar en un solo ``INSERT … SELECT … ON
    CONFLICT`` salió MÁS lento que el ``SELECT`` más el upsert, también para la
    venta que se omite (p95 ~14 ms contra ~5 ms)."""
    for tenant_id, items in por_tenant.items():
        ids = list(items)
        # Tres parámetros por producto: en tandas, lejos del tope de asyncpg
        # (32767) aunque un import venda decenas de miles de productos.
        for i in range(0, len(ids), TANDA_DE_DECISION):
            tanda = {pid: items[pid] for pid in ids[i : i + TANDA_DE_DECISION]}
            consulta = select(Product.id).where(_no_omitibles(tenant_id, tanda))
            for (pid,) in session.execute(consulta).tuples():
                _marcar(session, tenant_id, pid, tanda[pid])


def _al_chocar(stmt: Any, t: Table) -> Any:
    """Con la fila ya existente: sube la revisión (invalida un cálculo en curso)
    y deja ``pendiente_desde`` en el día menor."""
    actual = t.c.pendiente_desde
    return stmt.on_conflict_do_update(
        index_elements=[t.c.tenant_id, t.c.product_id],
        set_={
            "revision": t.c.revision + 1,
            "pendiente_desde": case(
                (actual.is_(None), stmt.excluded.pendiente_desde),
                (stmt.excluded.pendiente_desde < actual, stmt.excluded.pendiente_desde),
                else_=actual,
            ),
            # Conserva la marca más vieja todavía no publicada (antigüedad real).
            "marcado_en": case((actual.is_(None), stmt.excluded.marcado_en), else_=t.c.marcado_en),
        },
    )


def marcar_pendientes(session: Session, marcas: dict[tuple[uuid.UUID, uuid.UUID], date]) -> None:
    """Escribe las marcas (API sincrónica) con un upsert de varias filas por
    tanda: un import que marca 300 productos son 1 sentencia, no 300. Las filas
    van ordenadas por (tenant, producto), y Postgres las procesa en ese orden:
    dos transacciones que marcan los mismos productos no se bloquean en cruz."""
    if not marcas:
        return
    t = cast("Table", ProductCostState.__table__)
    insertar = pg_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert
    ahora = datetime.now(UTC)
    filas = [
        {
            "id": uuid.uuid4(),
            "tenant_id": tenant_id,
            "product_id": product_id,
            "revision": 1,
            "pendiente_desde": desde,
            "marcado_en": ahora,
            "intentos": 0,
            "stock_fuera_del_ledger": False,
            "aproximado": False,
        }
        for (tenant_id, product_id), desde in sorted(marcas.items(), key=_clave_de_orden)
    ]
    for i in range(0, len(filas), TANDA_DE_MARCAS):
        session.execute(_al_chocar(insertar(t).values(filas[i : i + TANDA_DE_MARCAS]), t))


def _clave_de_orden(item: tuple[tuple[uuid.UUID, uuid.UUID], date]) -> tuple[str, str]:
    (tenant_id, product_id), _ = item
    return str(tenant_id), str(product_id)


@event.listens_for(Session, "before_commit")
def _marcar_al_confirmar(session: Session) -> None:
    if session.in_nested_transaction():
        return
    session.flush()
    candidatas = _resolver_candidatas(session)
    # ``stock_units`` cambiado sin un movimiento (PATCH de stock, chat, dedup):
    # mueve ``stock_fuera_del_ledger``, copiado en todo el historial.
    con_mov: set[tuple[uuid.UUID, uuid.UUID]] = session.info.pop(_CON_MOVIMIENTO, set())
    for tenant_id, product_id in session.info.pop(_STOCK_TOCADO, set()) - con_mov:
        _marcar(session, tenant_id, product_id, TODO_EL_HISTORIAL)
    _marcar_no_omitibles(session, candidatas)
    marcas: dict[tuple[uuid.UUID, uuid.UUID], date] = session.info.pop(_MARCAS, {})
    if not marcas:
        return
    marcar_pendientes(session, marcas)
    session.info[_A_ENCOLAR] = sorted(marcas)


def _despachar(a_encolar: list[tuple[uuid.UUID, uuid.UUID]]) -> None:
    """Una tarea por tenant y tanda, sin reintentos de publicación: con el broker
    caído o lento no se espera. Lo que no se encola lo levanta el drenaje."""
    try:
        from app.jobs.recalcular_costo_promedio import (  # noqa: PLC0415
            recalcular_costo_promedio,
        )

        por_tenant: dict[uuid.UUID, list[str]] = {}
        for tenant_id, product_id in a_encolar:
            por_tenant.setdefault(tenant_id, []).append(str(product_id))
        for tenant_id, productos in por_tenant.items():
            for i in range(0, len(productos), TANDA_DE_ENCOLADO):
                recalcular_costo_promedio.apply_async(
                    args=(str(tenant_id), productos[i : i + TANDA_DE_ENCOLADO]),
                    retry=False,
                )
    except Exception as exc:  # noqa: BLE001 — la transacción ya commiteó
        logger.warning("costo_promedio.encolar_fallo", error=str(exc), productos=len(a_encolar))


@event.listens_for(Session, "after_commit")
def _encolar(session: Session) -> None:
    """Encola FUERA del event loop: la llamada al broker es sincrónica, y hacerla
    acá bloquearía a toda la API en cada commit que marca."""
    a_encolar: list[tuple[uuid.UUID, uuid.UUID]] = session.info.pop(_A_ENCOLAR, [])
    if not a_encolar:
        return
    if ENCOLAR_EN_HILO:
        threading.Thread(
            target=_despachar, args=(a_encolar,), name="encolar-costo-promedio", daemon=True
        ).start()
    else:
        _despachar(a_encolar)
