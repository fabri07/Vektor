"""M·R: el stock inicial importado como COMPRA deja de ser gasto.

Un catálogo confirmado con «La compré ahora» (``stock_treatment=purchase``) deja,
por producto, un movimiento ``purchase`` + ``catalog_initial_stock`` y un gasto
INVENTORY/COGS «Stock inicial: X» pagado por ``transfer``
(``ingestion_import_service._add_catalog_initial_stock_cogs``). Si esa mercadería
el negocio YA la tenía, el gasto es falso: infla la mercadería del período y resta
del banco una plata que nunca salió.

Esto anula SOLO el gasto (``REPAIR_MISCLASSIFIED_IMPORT``). El movimiento queda
como está — tipo, fecha y costo —, y con él ``stock_units``, ``unit_cost_ars``, el
costo de referencia, Proveedores, la integridad del inventario y los snapshots de
costo de las ventas. Convertir el movimiento a ``adjustment`` desataba una cascada
(Proveedores vacío, costo que cambia en el próximo recálculo, otra ancla en la
deduplicación); el dueño eligió no hacerlo (2026-10-12).

Además vuelve a «apertura» la elección guardada, en los dos lugares donde vive:
``parsed_summary_json["stock_treatment"]`` de los archivos reparados y TODAS las
decisiones recordadas «compra» del tenant (``ingestion_schema_decisions``). Si no,
«Volver a leer» volvería a crear los gastos. Las decisiones van por tenant y no
por archivo por dos razones: son por esquema (no por archivo) y el summary guardado
ya no trae ``mapping_contexts`` (el confirm lo compacta), así que la huella con que
se grabaron no se puede reconstruir. Una decisión recordada sólo precarga la
pantalla: lo peor que pasa es que la próxima hoja venga con «ya la tenía»
marcado, que es el default seguro.

No repara (y lo informa):

- gastos sin par exacto (archivo + fila + producto + importe), editados a mano o
  sin archivo de origen;
- archivos con una relectura APLICADA (deshacerla reviviría la generación
  anterior de gastos) o con una sesión de relectura abierta (su borrador puede
  traer «compra» y le gana al summary).

**Dry-run:** el llamador revierte la transacción. Con ``aplicar`` deja un
``data_repair_runs`` ``MR_STOCK_INICIAL_APERTURA`` con un item
``RECLASSIFY_EXPENSE`` por gasto y un ``decision_audit_log``; ``revertir`` revive
sólo lo que sigue tal como lo dejó la corrida. El lease de mantenimiento lo toma
el script; acá se toma el lock exclusivo de la transacción.

``tenant_id`` lo pasa el script (no hay JWT acá); cada consulta filtra por él.
"""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import maintenance_lock_service as mls
from app.application.services.deterministic_finance import calcular_flujo_neto_30d
from app.application.services.inventory_movement_origin import SOURCE_CATALOG_INITIAL_STOCK
from app.application.services.reread_service import (
    REPAIR_TYPE_REREAD,
    latest_applied_run_for_file,
)
from app.domain.business_time import today_ar
from app.domain.sale_unit import importe
from app.persistence.costo_promedio_pendiente import marcar_todo_el_historial
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.file import UploadedFile
from app.persistence.models.ingestion_schema_decision import IngestionSchemaDecision
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.repair import DataRepairItem, DataRepairRun
from app.persistence.models.transaction import ExpenseEntry

REPAIR_TYPE: Final = "MR_STOCK_INICIAL_APERTURA"
VOID_REASON: Final = "REPAIR_MISCLASSIFIED_IMPORT"
PREFIJO: Final = "Stock inicial: "
# Los mismos literales que ``ingestion_import_service.STOCK_TREATMENT_*`` (un test
# los ata): importar ese módulo acá arrastraría todo el pipeline de ingesta.
COMPRA: Final = "purchase"
APERTURA: Final = "opening_balance"
LOTE: Final = 500

#: Sesiones de relectura que todavía pueden aplicarse con su propio borrador.
#: ``RUNNING`` es el zombie del motor viejo (``_APPLY_STATUSES_STUCK``): también bloquea.
SESIONES_ABIERTAS: Final = (
    "PREVIEWING", "NEEDS_REVIEW", "READY_TO_APPLY", "QUEUED", "APPLYING", "RUNNING",
)

MOTIVO_SIN_ARCHIVO: Final = "sin archivo de origen"
MOTIVO_SIN_FILA: Final = "sin fila de origen"
MOTIVO_EDITADO: Final = "editado a mano"
MOTIVO_SIN_PAR: Final = "sin movimiento par"
MOTIVO_VARIOS_PARES: Final = "más de un movimiento para la misma fila"
MOTIVO_VARIOS_GASTOS: Final = "más de un gasto para la misma fila"
MOTIVO_OTRO_PRODUCTO: Final = "el movimiento es de otro producto"
MOTIVO_IMPORTE: Final = "el importe no es costo × cantidad del movimiento"
MOTIVO_RELECTURA: Final = "el archivo tiene una relectura aplicada (deshacerla revive los gastos)"
MOTIVO_SESION: Final = "el archivo tiene una relectura abierta"


@dataclass(frozen=True)
class Candidato:
    expense_id: uuid.UUID
    movement_id: uuid.UUID
    product_id: uuid.UUID
    file_id: uuid.UUID
    amount: Decimal
    transaction_date: datetime


@dataclass
class Archivo:
    file_id: uuid.UUID
    nombre: str | None
    #: El ``stock_treatment`` guardado en el summary, tal cual (str global del
    #: contrato viejo, dict por hoja o None).
    eleccion: Any
    bloqueo: str | None = None
    gastos: int = 0
    monto: Decimal = Decimal("0")


@dataclass
class Resumen:
    tenant_id: uuid.UUID
    candidatos: list[Candidato] = field(default_factory=list)
    excluidos: Counter[str] = field(default_factory=Counter)
    monto_excluido: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    archivos: dict[uuid.UUID, Archivo] = field(default_factory=dict)
    #: Decisiones recordadas (``ingestion_schema_decisions``) que dicen «compra».
    decisiones_compra: int = 0
    anulados: int = 0
    run_id: uuid.UUID | None = None
    flujo_antes: Decimal | None = None
    flujo_despues: Decimal | None = None

    @property
    def monto(self) -> Decimal:
        return sum((c.amount for c in self.candidatos), Decimal("0"))

    def monto_en_ventana(self, dias: int) -> Decimal:
        desde = today_ar() - timedelta(days=dias)
        return sum(
            (c.amount for c in self.candidatos if c.transaction_date.date() >= desde),
            Decimal("0"),
        )


def _excluir(r: Resumen, motivo: str, gasto: ExpenseEntry) -> None:
    r.excluidos[motivo] += 1
    r.monto_excluido[motivo] += gasto.amount


def _sin_compra(eleccion: Any) -> Any:
    """La elección guardada con cada «compra» pasada a «apertura»."""
    if isinstance(eleccion, dict):
        return {k: (APERTURA if v == COMPRA else v) for k, v in eleccion.items()}
    return APERTURA if eleccion == COMPRA else eleccion


async def _decisiones_compra(
    session: AsyncSession, tenant_id: uuid.UUID
) -> list[IngestionSchemaDecision]:
    """Las decisiones recordadas «compra» del tenant (ver el docstring del módulo)."""
    filas = await session.execute(
        select(IngestionSchemaDecision).where(
            IngestionSchemaDecision.tenant_id == tenant_id,
            IngestionSchemaDecision.decision_type == "stock_treatment",
        )
    )
    return [d for d in filas.scalars() if (d.payload or {}).get("treatment") == COMPRA]


async def _bloqueo(session: AsyncSession, tenant_id: uuid.UUID, file_id: uuid.UUID) -> str | None:
    if await latest_applied_run_for_file(session, file_id, tenant_id) is not None:
        return MOTIVO_RELECTURA
    abiertas = await session.execute(
        select(DataRepairRun.details_json).where(
            DataRepairRun.tenant_id == tenant_id,
            DataRepairRun.repair_type == REPAIR_TYPE_REREAD,
            DataRepairRun.status.in_(SESIONES_ABIERTAS),
        )
    )
    # Filtro por ``file_id`` en Python, como ``reread_service`` (portable a SQLite).
    if any((d or {}).get("file_id") == str(file_id) for d in abiertas.scalars()):
        return MOTIVO_SESION
    return None


async def analizar(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    file_ids: set[uuid.UUID] | None = None,
) -> Resumen:
    """Arma el par gasto ↔ movimiento de cada «Stock inicial» vivo. Sólo lee."""
    r = Resumen(tenant_id=tenant_id)
    gastos = list(
        (
            await session.execute(
                select(ExpenseEntry)
                .where(
                    ExpenseEntry.tenant_id == tenant_id,
                    ExpenseEntry.voided_at.is_(None),
                    ExpenseEntry.category == "INVENTORY",
                    ExpenseEntry.expense_type == "COGS",
                    ExpenseEntry.product_id.is_not(None),
                    ExpenseEntry.description.like(f"{PREFIJO}%"),
                )
                .order_by(ExpenseEntry.transaction_date, ExpenseEntry.id)
            )
        ).scalars()
    )
    if file_ids is not None:
        gastos = [g for g in gastos if g.source_upload_id in file_ids]

    ids_archivo = {g.source_upload_id for g in gastos if g.source_upload_id is not None}
    movimientos: dict[tuple[uuid.UUID, str], list[InventoryMovement]] = defaultdict(list)
    if ids_archivo:
        for m in (
            await session.execute(
                select(InventoryMovement).where(
                    InventoryMovement.tenant_id == tenant_id,
                    InventoryMovement.voided_at.is_(None),
                    InventoryMovement.movement_type == "purchase",
                    InventoryMovement.source_type == SOURCE_CATALOG_INITIAL_STOCK,
                    InventoryMovement.source_upload_id.in_(ids_archivo),
                )
            )
        ).scalars():
            if m.source_upload_id is not None and m.source_row_ref is not None:
                movimientos[(m.source_upload_id, m.source_row_ref)].append(m)
        for f in (
            await session.execute(
                select(UploadedFile).where(
                    UploadedFile.tenant_id == tenant_id, UploadedFile.id.in_(ids_archivo)
                )
            )
        ).scalars():
            summary = f.parsed_summary_json if isinstance(f.parsed_summary_json, dict) else {}
            r.archivos[f.id] = Archivo(
                file_id=f.id,
                nombre=f.original_filename,
                eleccion=summary.get("stock_treatment"),
                bloqueo=await _bloqueo(session, tenant_id, f.id),
            )

    r.decisiones_compra = len(await _decisiones_compra(session, tenant_id))

    # ``list`` antes de ``dict``: un Result tiene ``keys()`` y ``dict`` lo
    # trataría como mapping.
    ids_producto = {g.product_id for g in gastos if g.product_id is not None}
    filas_factor = (
        await session.execute(
            select(Product.id, Product.base_units_per_sale_unit).where(
                Product.tenant_id == tenant_id, Product.id.in_(ids_producto)
            )
        )
    ).tuples()
    factores = dict(list(filas_factor))
    por_fila = Counter(
        (g.source_upload_id, g.source_row_ref)
        for g in gastos
        if g.source_upload_id is not None and g.source_row_ref is not None
    )
    for g in gastos:
        if g.source_upload_id is None:
            _excluir(r, MOTIVO_SIN_ARCHIVO, g)
            continue
        archivo = r.archivos.get(g.source_upload_id)
        if archivo is not None and archivo.bloqueo is not None:
            _excluir(r, archivo.bloqueo, g)
            continue
        if g.source_row_ref is None:
            _excluir(r, MOTIVO_SIN_FILA, g)
            continue
        if g.has_user_edits:
            _excluir(r, MOTIVO_EDITADO, g)
            continue
        clave = (g.source_upload_id, g.source_row_ref)
        if por_fila[clave] > 1:
            _excluir(r, MOTIVO_VARIOS_GASTOS, g)
            continue
        pares = movimientos.get(clave, [])
        if not pares:
            _excluir(r, MOTIVO_SIN_PAR, g)
            continue
        if len(pares) > 1:
            _excluir(r, MOTIVO_VARIOS_PARES, g)
            continue
        m = pares[0]
        if m.product_id != g.product_id:
            _excluir(r, MOTIVO_OTRO_PRODUCTO, g)
            continue
        if not _importe_coincide(g.amount, m, factores.get(m.product_id) or 1):
            _excluir(r, MOTIVO_IMPORTE, g)
            continue
        assert g.product_id is not None
        r.candidatos.append(
            Candidato(
                expense_id=g.id,
                movement_id=m.id,
                product_id=g.product_id,
                file_id=g.source_upload_id,
                amount=g.amount,
                transaction_date=g.transaction_date,
            )
        )
        if archivo is not None:
            archivo.gastos += 1
            archivo.monto += g.amount
    return r


def _importe_coincide(monto: Decimal, m: InventoryMovement, factor: int) -> bool:
    """¿El gasto es costo × cantidad del movimiento?

    El gasto se calculó con el costo COMPLETO del archivo, pero
    ``InventoryMovement.unit_cost`` es ``Numeric(12,2)``: un costo de 123,456 se
    guarda 123,46. Se tolera ese redondeo (medio centavo por unidad) más un
    centavo del redondeo del importe. El alta multihoja de un producto nuevo no
    pasaba el factor: se prueba con el del producto y con 1.
    """
    if m.unit_cost is None or m.qty <= 0:
        return False
    for f in {factor, 1}:
        f = f if f > 0 else 1
        tolerancia = Decimal("0.005") * m.qty / f + Decimal("0.01")
        if abs(monto - importe(m.unit_cost, m.qty, f)) <= tolerancia:
            return True
    return False


def _foto(g: ExpenseEntry) -> dict[str, Any]:
    return {
        "voided_at": g.voided_at.isoformat() if g.voided_at else None,
        "void_reason": g.void_reason,
        "voided_by_repair_run_id": (
            str(g.voided_by_repair_run_id) if g.voided_by_repair_run_id else None
        ),
    }


async def reparar(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    aplicar: bool,
    file_ids: set[uuid.UUID] | None = None,
) -> Resumen:
    """Anula los gastos con par exacto y vuelve a «apertura» la elección guardada.

    Escribe SIEMPRE (así el dry-run mide el efecto real); sin ``aplicar`` el
    llamador revierte la transacción y no se toma el lock.
    """
    if aplicar:
        await mls.acquire_maintenance_lock_exclusive(session, tenant_id)
    r = await analizar(session, tenant_id, file_ids)
    r.flujo_antes = (await calcular_flujo_neto_30d(tenant_id, session))["flujo_neto"]
    if not r.candidatos:
        r.flujo_despues = r.flujo_antes
        return r

    run = DataRepairRun(
        tenant_id=tenant_id,
        repair_type=REPAIR_TYPE,
        status="RUNNING",
        dry_run=not aplicar,
        candidates_found=len(r.candidatos),
    )
    session.add(run)
    await session.flush()
    r.run_id = run.id
    ahora = datetime.now(UTC)
    por_gasto = {c.expense_id: c for c in r.candidatos}
    ids = list(por_gasto)
    for i in range(0, len(ids), LOTE):
        # Bloquea el lote y vuelve a filtrar: un gasto anulado o editado desde
        # que se listó no se toca.
        lote = (
            await session.execute(
                select(ExpenseEntry)
                .where(
                    ExpenseEntry.id.in_(ids[i : i + LOTE]),
                    ExpenseEntry.tenant_id == tenant_id,
                    ExpenseEntry.voided_at.is_(None),
                    ExpenseEntry.has_user_edits.is_(False),
                )
                .with_for_update()
            )
        ).scalars()
        for g in lote:
            c = por_gasto[g.id]
            antes = _foto(g)
            g.voided_at = ahora
            g.void_reason = VOID_REASON
            g.voided_by_repair_run_id = run.id
            r.anulados += 1
            session.add(
                DataRepairItem(
                    run_id=run.id,
                    tenant_id=tenant_id,
                    source_file_id=c.file_id,
                    product_id=c.product_id,
                    action="RECLASSIFY_EXPENSE",
                    before_json={
                        "id": str(g.id),
                        "expense_id": str(g.id),
                        "movement_id": str(c.movement_id),
                        "amount": str(g.amount),
                        **antes,
                    },
                    after_json={"id": str(g.id), **_foto(g)},
                    confidence="HIGH",
                )
            )
        await session.flush()

    elecciones: dict[str, dict[str, Any]] = {}
    decisiones: list[dict[str, Any]] = []
    archivos_reparados = {c.file_id for c in r.candidatos}
    for f in (
        await session.execute(
            select(UploadedFile)
            .where(UploadedFile.tenant_id == tenant_id, UploadedFile.id.in_(archivos_reparados))
            .with_for_update()
        )
    ).scalars():
        if not isinstance(f.parsed_summary_json, dict):
            continue
        summary = f.parsed_summary_json
        eleccion = summary.get("stock_treatment")
        nueva = _sin_compra(eleccion)
        if nueva != eleccion:
            # Dict nuevo: la columna JSONB no detecta mutaciones en el lugar.
            f.parsed_summary_json = {**summary, "stock_treatment": nueva}
            elecciones[str(f.id)] = {"antes": eleccion, "despues": nueva}
    for d in await _decisiones_compra(session, tenant_id):
        antes_d = dict(d.payload)
        d.payload = {**d.payload, "treatment": APERTURA}
        decisiones.append({"id": str(d.id), "antes": antes_d, "despues": d.payload})
    await session.flush()

    r.flujo_despues = (await calcular_flujo_neto_30d(tenant_id, session))["flujo_neto"]
    # M·P2b: anular estos gastos convierte su compra de catálogo en apertura en
    # el costo promedio (``catalogo_reparado``): cambia la clase y el orden de los
    # registros ya publicados de esos productos.
    marcar_todo_el_historial(session, tenant_id, {c.product_id for c in r.candidatos})
    run.status = "APPLIED"
    run.completed_at = datetime.now(UTC)
    run.details_json = {
        "gastos_anulados": r.anulados,
        "monto_anulado": str(r.monto),
        "excluidos": dict(r.excluidos),
        "elecciones": elecciones,
        "decisiones": decisiones,
        "flujo_neto_30d_antes": str(r.flujo_antes),
        "flujo_neto_30d_despues": str(r.flujo_despues),
    }
    session.add(
        DecisionAuditLog(
            tenant_id=tenant_id,
            decision_type="REPAIR_OPENING_STOCK_EXPENSES",
            decision_data={
                "run_id": str(run.id),
                "repair_type": REPAIR_TYPE,
                "gastos_anulados": r.anulados,
                "monto_anulado": str(r.monto),
                "archivos": sorted(str(a) for a in archivos_reparados),
            },
            triggered_by="script:reparar_stock_inicial_como_compra",
            context={"accion": "apply" if aplicar else "dry_run"},
            created_at=datetime.now(UTC),
        )
    )
    await session.flush()
    return r


@dataclass
class Reversion:
    revividos: int = 0
    salteados: int = 0
    elecciones: int = 0
    decisiones: int = 0


async def revertir(session: AsyncSession, run_id: uuid.UUID) -> Reversion:
    """Revive los gastos de la corrida que siguen anulados por ella y devuelve la
    elección guardada que sigue tal como la dejó. Lo demás se saltea."""
    run = await session.get(DataRepairRun, run_id, with_for_update=True)
    if run is None or run.repair_type != REPAIR_TYPE or run.tenant_id is None:
        raise ValueError(f"No es una corrida de {REPAIR_TYPE}: {run_id}")
    res = Reversion()
    if run.status == "REVERTED":
        return res
    tenant_id = run.tenant_id
    await mls.acquire_maintenance_lock_exclusive(session, tenant_id)
    items = list(
        (
            await session.execute(select(DataRepairItem).where(DataRepairItem.run_id == run_id))
        ).scalars()
    )
    par = {
        uuid.UUID(it.before_json["expense_id"]): (
            uuid.UUID(it.before_json["movement_id"]),
            it.source_file_id,
        )
        for it in items
        if it.before_json
    }
    ids = list(par)
    # Un movimiento anulado (una relectura o un borrado posterior reemplazó esa
    # generación) deja su gasto muerto: revivirlo crearía un gasto sin par que
    # ni la relectura ni su deshacer vuelven a anular.
    vivos = set(
        (
            await session.execute(
                select(InventoryMovement.id).where(
                    InventoryMovement.tenant_id == tenant_id,
                    InventoryMovement.id.in_([m for m, _ in par.values()]),
                    InventoryMovement.voided_at.is_(None),
                )
            )
        ).scalars()
    )
    archivos_reemplazados: set[uuid.UUID] = set()
    gastos: dict[uuid.UUID, ExpenseEntry] = {}
    for i in range(0, len(ids), LOTE):
        for g in (
            await session.execute(
                select(ExpenseEntry)
                .where(ExpenseEntry.id.in_(ids[i : i + LOTE]), ExpenseEntry.tenant_id == tenant_id)
                .with_for_update()
            )
        ).scalars():
            gastos[g.id] = g
    for gid in ids:
        gasto = gastos.get(gid)
        movimiento, archivo = par[gid]
        if movimiento not in vivos:
            if archivo is not None:
                archivos_reemplazados.add(archivo)
            res.salteados += 1
            continue
        if (
            gasto is None
            or gasto.voided_by_repair_run_id != run.id
            or gasto.void_reason != VOID_REASON
        ):
            res.salteados += 1
            continue
        gasto.voided_at = None
        gasto.void_reason = None
        gasto.voided_by_repair_run_id = None
        res.revividos += 1

    detalles = run.details_json or {}
    elecciones: dict[str, dict[str, Any]] = detalles.get("elecciones") or {}
    if elecciones:
        for f in (
            await session.execute(
                select(UploadedFile)
                .where(
                    UploadedFile.tenant_id == tenant_id,
                    UploadedFile.id.in_([uuid.UUID(k) for k in elecciones]),
                )
                .with_for_update()
            )
        ).scalars():
            if f.id in archivos_reemplazados:
                continue
            cambio = elecciones[str(f.id)]
            summary = f.parsed_summary_json if isinstance(f.parsed_summary_json, dict) else None
            if summary is None or summary.get("stock_treatment") != cambio["despues"]:
                continue
            f.parsed_summary_json = {**summary, "stock_treatment": cambio["antes"]}
            res.elecciones += 1
    for cambio in detalles.get("decisiones") or []:
        d = await session.get(IngestionSchemaDecision, uuid.UUID(cambio["id"]))
        if d is None or d.tenant_id != tenant_id or d.payload != cambio["despues"]:
            continue
        d.payload = cambio["antes"]
        res.decisiones += 1

    # M·P2b: revivir los gastos vuelve esas aperturas a compras en el promedio.
    marcar_todo_el_historial(session, tenant_id, {it.product_id for it in items})
    run.status = "REVERTED"
    session.add(
        DecisionAuditLog(
            tenant_id=tenant_id,
            decision_type="REPAIR_OPENING_STOCK_EXPENSES_REVERTED",
            decision_data={
                "run_id": str(run.id),
                "revividos": res.revividos,
                "salteados": res.salteados,
                "elecciones": res.elecciones,
                "decisiones": res.decisiones,
            },
            triggered_by="script:reparar_stock_inicial_como_compra",
            context={"accion": "revert"},
            created_at=datetime.now(UTC),
        )
    )
    await session.flush()
    return res
