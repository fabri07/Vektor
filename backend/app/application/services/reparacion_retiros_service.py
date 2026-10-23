"""D·3: los retiros viejos cargados como gasto pasan a ser retiros de capital.

Antes de D·2 (2026-10-10), ``POST /expenses/profit-withdrawal`` dejaba un gasto
PAYROLL/OPEX «Retiro de ganancias anticipadas» con
``custom_fields.profit_withdrawal = true``. Un retiro del socio no es un gasto del
negocio: restaba del margen neto, de los gastos por categoría, del score y del
Resultado del resumen económico. Esto anula ese gasto y crea, en la MISMA
transacción, el ``retiro`` de capital en dinero equivalente
(``source_type='migrado_de_gasto'``, ``source_ref`` = id del gasto).

**Qué se repara.** Sólo gastos vivos con la marca y sin archivo de origen (lo que
escribía esa ruta). Nunca se infiere por la descripción ni por la categoría: un
sueldo del dueño cargado como PAYROLL común no se detecta. No se toca (y se
informa con su motivo): importe ≤ 0, medio de pago que el capital no admite (no
se mapea a «other»: movería la caja), o un gasto que ya tiene su movimiento
migrado vivo. Un gasto editado después del alta entra con sus valores actuales,
que son los que hoy restan, y se cuenta aparte.

**La caja no cambia.** El retiro sale del mismo medio, el mismo día y por el mismo
importe. Antes de dar por buena la corrida se compara, por cada fecha afectada,
lo que sale de cada medio (gastos vivos más capital en dinero, en centavos), el
esperado del arqueo de ese día y la proyección de caja. Si algo difiere, se
levanta ``CajaCambioError`` y el llamador revierte TODA la corrida del tenant.

**Modos.** ``analizar`` sólo lee (es el informe de ``--all``). ``reparar`` corre
en una sola transacción por tenant, con el lock exclusivo de mantenimiento y los
gastos ``FOR UPDATE``, y revalida todos los criterios bajo el bloqueo
(``source_ref`` no tiene restricción única: el lock y la revalidación evitan un
doble movimiento). Sin ``aplicar`` es una simulación: el llamador hace rollback.
``revertir`` deshace de a PARES (gasto + movimiento) indivisibles.

``tenant_id`` lo pasa el script (no hay JWT acá); cada consulta filtra por él.
"""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import maintenance_lock_service as mls
from app.application.services.cash_close_service import CashCloseService
from app.application.services.economic_summary_service import compute_economic_summary
from app.application.services.facts_provider import margenes_del_tenant
from app.application.services.forecast_service import _compute_forecast
from app.domain.business_time import today_ar
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.owner_capital import (
    FORM_DINERO,
    KIND_RETIRO,
    PAYMENT_METHODS,
    OwnerCapitalMovement,
)
from app.persistence.models.repair import DataRepairItem, DataRepairRun
from app.persistence.models.transaction import ExpenseEntry
from app.persistence.repositories.owner_capital_repository import flujo_de_dinero

REPAIR_TYPE: Final = "D3_RETIRO_A_CAPITAL"
#: Un motivo que ya admite el CHECK; ``voided_by_repair_run_id`` identifica la corrida.
VOID_REASON: Final = "MANUAL_ADMIN_VOID"
SOURCE_MIGRADO: Final = "migrado_de_gasto"
MARCA: Final = "profit_withdrawal"
VOID_REASON_CAPITAL: Final = "D·3 revertido: el retiro vuelve a ser el gasto original"
TRIGGERED_BY: Final = "script:reparar_retiros_a_capital"

MOTIVO_IMPORTE: Final = "importe no positivo"
MOTIVO_MEDIO: Final = "medio de pago que el capital no admite"
MOTIVO_YA_MIGRADO: Final = "ya tiene su retiro de capital migrado"
#: Una fecha futura nunca se persiste (``occurred_on`` heredaría la del gasto).
MOTIVO_FECHA_FUTURA: Final = "fecha futura"

MOTIVO_PAR_GASTO: Final = "el gasto ya no está como lo dejó la corrida"
MOTIVO_PAR_CAPITAL: Final = "el retiro de capital ya no está como lo dejó la corrida"
#: No se reintenta: revivir el gasto sería deshacer la anulación de un usuario.
MOTIVO_PAR_ANULADO: Final = "un usuario anuló el retiro migrado (no se reintenta)"

CENTAVO: Final = Decimal("0.01")


class CajaCambioError(Exception):
    """La caja no dio igual antes y después: la corrida no se puede confirmar."""

    def __init__(self, diferencias: list[str]) -> None:
        super().__init__("; ".join(diferencias[:5]))
        self.diferencias = diferencias


@dataclass(frozen=True)
class Candidato:
    expense_id: uuid.UUID
    amount: Decimal
    dia: date
    medio: str
    notes: str | None
    editado: bool


@dataclass
class Resumen:
    tenant_id: uuid.UUID
    candidatos: list[Candidato] = field(default_factory=list)
    excluidos: Counter[str] = field(default_factory=Counter)
    monto_excluido: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    run_id: uuid.UUID | None = None
    #: Antes y después (sólo ``reparar``): margen neto del último mes cerrado y
    #: Resultado del resumen económico entre la primera y la última fecha.
    efecto: dict[str, Any] = field(default_factory=dict)

    @property
    def monto(self) -> Decimal:
        return sum((c.amount for c in self.candidatos), Decimal("0"))

    @property
    def editados(self) -> int:
        return sum(1 for c in self.candidatos if c.editado)

    def por_mes(self) -> dict[str, Decimal]:
        out: dict[str, Decimal] = defaultdict(Decimal)
        for c in self.candidatos:
            out[c.dia.strftime("%Y-%m")] += c.amount
        return dict(sorted(out.items()))

    def por_medio(self) -> dict[str, Decimal]:
        out: dict[str, Decimal] = defaultdict(Decimal)
        for c in self.candidatos:
            out[c.medio] += c.amount
        return dict(sorted(out.items()))


def _centavos(v: Any) -> Decimal:
    return Decimal(str(v or 0)).quantize(CENTAVO, rounding=ROUND_HALF_UP)


def _dia(g: ExpenseEntry) -> date:
    t = g.transaction_date
    return t.date() if isinstance(t, datetime) else t


def es_retiro_marcado(g: ExpenseEntry) -> bool:
    """Lo que escribía ``profit-withdrawal`` antes de D·2, todavía vivo."""
    return (
        g.voided_at is None
        and g.source_upload_id is None
        and (g.custom_fields or {}).get(MARCA) is True
    )


def motivo_de_exclusion(g: ExpenseEntry, migrados: set[str]) -> str | None:
    if g.amount is None or g.amount <= 0:
        return MOTIVO_IMPORTE
    if g.payment_method not in PAYMENT_METHODS:
        return MOTIVO_MEDIO
    if _dia(g) > today_ar():
        return MOTIVO_FECHA_FUTURA
    if str(g.id) in migrados:
        return MOTIVO_YA_MIGRADO
    return None


async def _gastos_marcados(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    desde: date | None,
    hasta: date | None,
    *,
    bloquear: bool,
) -> list[ExpenseEntry]:
    e = ExpenseEntry
    consulta = select(e).where(
        e.tenant_id == tenant_id, e.voided_at.is_(None), e.source_upload_id.is_(None)
    )
    if desde is not None:
        consulta = consulta.where(func.date(e.transaction_date) >= desde)
    if hasta is not None:
        consulta = consulta.where(func.date(e.transaction_date) <= hasta)
    consulta = consulta.order_by(e.id)
    if bloquear:
        consulta = consulta.with_for_update()
    # La marca vive en un JSON: se filtra acá, portable entre motores. Los gastos
    # manuales vivos de un tenant son pocos.
    return [g for g in (await session.execute(consulta)).scalars() if es_retiro_marcado(g)]


async def _migrados(session: AsyncSession, tenant_id: uuid.UUID, ids: list[str]) -> set[str]:
    if not ids:
        return set()
    c = OwnerCapitalMovement
    filas = await session.execute(
        select(c.source_ref).where(
            c.tenant_id == tenant_id,
            c.source_type == SOURCE_MIGRADO,
            c.voided_at.is_(None),
            c.source_ref.in_(ids),
        )
    )
    return {r for r in filas.scalars() if r}


async def _editados(session: AsyncSession, tenant_id: uuid.UUID, ids: set[str]) -> set[str]:
    filas = await session.execute(
        select(DecisionAuditLog.decision_data).where(
            DecisionAuditLog.tenant_id == tenant_id,
            DecisionAuditLog.decision_type == "DATA_RECORD_UPDATED",
        )
    )
    out: set[str] = set()
    for d in filas.scalars():
        if (d or {}).get("record_type") == "expense" and (d or {}).get("record_id") in ids:
            out.add(str(d["record_id"]))
    return out


async def _clasificar(
    session: AsyncSession, tenant_id: uuid.UUID, gastos: list[ExpenseEntry]
) -> tuple[Resumen, list[ExpenseEntry]]:
    r = Resumen(tenant_id=tenant_id)
    ids = [str(g.id) for g in gastos]
    migrados = await _migrados(session, tenant_id, ids)
    editados = await _editados(session, tenant_id, set(ids))
    elegibles: list[ExpenseEntry] = []
    for g in gastos:
        motivo = motivo_de_exclusion(g, migrados)
        if motivo is not None:
            r.excluidos[motivo] += 1
            r.monto_excluido[motivo] += g.amount or Decimal("0")
            continue
        elegibles.append(g)
        r.candidatos.append(
            Candidato(
                expense_id=g.id,
                amount=g.amount,
                dia=_dia(g),
                medio=g.payment_method,
                notes=g.notes,
                editado=str(g.id) in editados,
            )
        )
    return r, elegibles


async def analizar(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    desde: date | None = None,
    hasta: date | None = None,
) -> Resumen:
    """Los retiros viejos del tenant y lo que se excluiría. SÓLO LEE, sin locks."""
    gastos = await _gastos_marcados(session, tenant_id, desde, hasta, bloquear=False)
    r, _ = await _clasificar(session, tenant_id, gastos)
    return r


# ── La caja ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FotoDeCaja:
    #: (día, medio) → salida neta de dinero (gastos vivos + capital), en centavos.
    salidas: dict[tuple[date, str], Decimal]
    #: día → esperado del arqueo (total y por medio), en centavos.
    arqueo: dict[date, tuple[Decimal, dict[str, Decimal]]]
    proyeccion: Any


async def foto_de_caja(
    session: AsyncSession, tenant_id: uuid.UUID, dias: set[date]
) -> FotoDeCaja:
    """Lo que la caja ve en cada día afectado: por medio de pago, el arqueo y la
    proyección. Lee la transacción en curso (la simulación también)."""
    salidas: dict[tuple[date, str], Decimal] = defaultdict(Decimal)
    if not dias:
        return FotoDeCaja({}, {}, None)
    e = ExpenseEntry
    dia_sql = func.date(e.transaction_date)
    filas = await session.execute(
        select(dia_sql, e.payment_method, func.sum(e.amount))
        .where(
            e.tenant_id == tenant_id,
            e.voided_at.is_(None),
            dia_sql >= min(dias),
            dia_sql <= max(dias),
        )
        .group_by(dia_sql, e.payment_method)
    )
    for dia_crudo, medio, total in filas.tuples():
        # SQLite devuelve ``func.date`` como texto.
        dia = dia_crudo if isinstance(dia_crudo, date) else date.fromisoformat(str(dia_crudo))
        if dia in dias:
            salidas[(dia, str(medio))] += _centavos(total)
    for f in await flujo_de_dinero(session, tenant_id, min(dias), max(dias)):
        if f.dia in dias:
            salidas[(f.dia, f.medio)] += _centavos(f.salidas) - _centavos(f.entradas)
    arqueo: dict[date, tuple[Decimal, dict[str, Decimal]]] = {}
    servicio = CashCloseService(session)
    for dia in sorted(dias):
        total, por_medio = await servicio._compute_expected(tenant_id, dia)
        arqueo[dia] = (_centavos(total), {k: _centavos(v) for k, v in por_medio.items()})
    proyeccion = await _compute_forecast(tenant_id, session)
    return FotoDeCaja(
        {k: v for k, v in salidas.items() if v != 0}, arqueo, proyeccion
    )


def diferencias_de_caja(antes: FotoDeCaja, despues: FotoDeCaja) -> list[str]:
    out: list[str] = []
    for clave in sorted(set(antes.salidas) | set(despues.salidas)):
        a, d = antes.salidas.get(clave, Decimal(0)), despues.salidas.get(clave, Decimal(0))
        if a != d:
            out.append(f"salidas {clave[0]} {clave[1]}: {a} → {d}")
    for dia in sorted(set(antes.arqueo) | set(despues.arqueo)):
        if antes.arqueo.get(dia) != despues.arqueo.get(dia):
            out.append(f"arqueo {dia}: {antes.arqueo.get(dia)} → {despues.arqueo.get(dia)}")
    if antes.proyeccion != despues.proyeccion:
        out.append("proyección de caja")
    return out


async def _efecto(
    session: AsyncSession, tenant_id: uuid.UUID, dias: set[date]
) -> dict[str, Any]:
    margenes = await margenes_del_tenant(session, tenant_id)
    neto = margenes.neto if margenes is not None else None
    resumen = await compute_economic_summary(session, tenant_id, min(dias), max(dias))
    return {
        "margen_neto_pct": None if neto is None else neto.value,
        "margen_neto_periodo": None if neto is None else neto.period,
        "margen_neto_estado": None if neto is None else (neto.detail or {}).get("estado"),
        "margen_neto_motivo": None if neto is None else (neto.detail or {}).get("motivo"),
        "resultado_ars": str(resumen.get("net_result_ars")),
    }


# ── Reparar ──────────────────────────────────────────────────────────────────


def _foto_gasto(g: ExpenseEntry) -> dict[str, Any]:
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
    desde: date | None = None,
    hasta: date | None = None,
) -> Resumen:
    """Convierte los retiros del tenant en UNA transacción. No commitea.

    Sin ``aplicar`` es una simulación: escribe para medir el efecto real y el
    llamador revierte. Levanta ``CajaCambioError`` si la caja no da igual.
    """
    await mls.acquire_maintenance_lock_exclusive(session, tenant_id)
    # Bajo el lock y ``FOR UPDATE``: todos los criterios se vuelven a evaluar acá,
    # no se confía en un análisis previo.
    gastos = await _gastos_marcados(session, tenant_id, desde, hasta, bloquear=True)
    r, elegibles = await _clasificar(session, tenant_id, gastos)
    if not elegibles:
        return r
    dias = {c.dia for c in r.candidatos}
    caja_antes = await foto_de_caja(session, tenant_id, dias)
    r.efecto["antes"] = await _efecto(session, tenant_id, dias)

    run = DataRepairRun(
        tenant_id=tenant_id,
        repair_type=REPAIR_TYPE,
        status="RUNNING",
        dry_run=not aplicar,
        candidates_found=len(elegibles),
    )
    session.add(run)
    await session.flush()
    r.run_id = run.id
    ahora = datetime.now(UTC)
    por_id = {c.expense_id: c for c in r.candidatos}
    for g in elegibles:
        c = por_id[g.id]
        antes = _foto_gasto(g)
        capital = OwnerCapitalMovement(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            kind=KIND_RETIRO,
            form=FORM_DINERO,
            amount_ars=c.amount,
            occurred_on=c.dia,
            payment_method=c.medio,
            notes=c.notes,
            source_type=SOURCE_MIGRADO,
            source_ref=str(g.id),
            created_by_user_id=None,
        )
        session.add(capital)
        g.voided_at = ahora
        g.void_reason = VOID_REASON
        g.voided_by_repair_run_id = run.id
        session.add(
            DataRepairItem(
                run_id=run.id,
                tenant_id=tenant_id,
                action="RECLASSIFY_EXPENSE",
                before_json={
                    "id": str(g.id),
                    "expense_id": str(g.id),
                    "capital_id": str(capital.id),
                    "amount": str(c.amount),
                    "payment_method": c.medio,
                    "dia": c.dia.isoformat(),
                    "editado": c.editado,
                    **antes,
                },
                after_json={"id": str(g.id), "capital_id": str(capital.id), **_foto_gasto(g)},
                confidence="HIGH",
            )
        )
    await session.flush()

    diferencias = diferencias_de_caja(caja_antes, await foto_de_caja(session, tenant_id, dias))
    if diferencias:
        raise CajaCambioError(diferencias)
    r.efecto["despues"] = await _efecto(session, tenant_id, dias)
    run.status = "APPLIED"
    run.completed_at = datetime.now(UTC)
    run.details_json = {
        "convertidos": len(elegibles),
        "monto": str(r.monto),
        "editados": r.editados,
        "por_mes": {k: str(v) for k, v in r.por_mes().items()},
        "excluidos": dict(r.excluidos),
        "efecto": r.efecto,
        "desde": desde.isoformat() if desde else None,
        "hasta": hasta.isoformat() if hasta else None,
    }
    session.add(
        DecisionAuditLog(
            tenant_id=tenant_id,
            decision_type="REPAIR_PROFIT_WITHDRAWAL_TO_CAPITAL",
            decision_data={
                "run_id": str(run.id),
                "repair_type": REPAIR_TYPE,
                "convertidos": len(elegibles),
                "monto": str(r.monto),
                "gastos": [str(g.id) for g in elegibles],
            },
            triggered_by=TRIGGERED_BY,
            context={"accion": "apply" if aplicar else "simulacion"},
            created_at=datetime.now(UTC),
        )
    )
    await session.flush()
    return r


# ── Revertir ─────────────────────────────────────────────────────────────────


@dataclass
class Reversion:
    revertidos: list[str] = field(default_factory=list)
    #: gasto → motivo de los pares que no se pudieron revertir (se reintentan).
    pendientes: dict[str, str] = field(default_factory=dict)

    @property
    def completa(self) -> bool:
        return not self.pendientes

    @property
    def reintentables(self) -> dict[str, str]:
        return {k: v for k, v in self.pendientes.items() if v != MOTIVO_PAR_ANULADO}


def _par_intacto(
    g: ExpenseEntry | None,
    cap: OwnerCapitalMovement | None,
    antes: dict[str, Any],
    run_id: uuid.UUID,
) -> str | None:
    """``None`` si el par sigue exactamente como lo dejó la corrida."""
    if (
        g is None
        or g.voided_at is None
        or g.voided_by_repair_run_id != run_id
        or g.void_reason != VOID_REASON
        or _centavos(g.amount) != _centavos(antes["amount"])
        or g.payment_method != antes["payment_method"]
        or _dia(g).isoformat() != antes["dia"]
    ):
        return MOTIVO_PAR_GASTO
    if cap is not None and cap.voided_at is not None:
        return MOTIVO_PAR_ANULADO
    if (
        cap is None
        or cap.kind != KIND_RETIRO
        or cap.form != FORM_DINERO
        or cap.source_type != SOURCE_MIGRADO
        or cap.source_ref != str(g.id)
        or _centavos(cap.amount_ars) != _centavos(antes["amount"])
        or cap.payment_method != antes["payment_method"]
        or cap.occurred_on.isoformat() != antes["dia"]
    ):
        return MOTIVO_PAR_CAPITAL
    return None


async def revertir(session: AsyncSession, run_id: uuid.UUID) -> Reversion:
    """Deshace la corrida de a pares indivisibles, en UNA transacción. No commitea.

    Un par se revierte sólo si el gasto y el movimiento siguen exactamente como los
    dejó la corrida; si no, se saltea ENTERO (nunca queda el retiro duplicado ni
    desaparece de la caja) y queda pendiente: volver a correr reintenta sólo esos.
    Levanta ``CajaCambioError`` si la caja no da igual.
    """
    # ``populate_existing``: el script pudo leer el run antes (sin lock).
    run = await session.get(
        DataRepairRun, run_id, with_for_update=True, populate_existing=True
    )
    if run is None or run.repair_type != REPAIR_TYPE or run.tenant_id is None:
        raise ValueError(f"No es una corrida de {REPAIR_TYPE}: {run_id}")
    res = Reversion()
    if run.status == "REVERTED" or run.dry_run:
        return res
    tenant_id = run.tenant_id
    await mls.acquire_maintenance_lock_exclusive(session, tenant_id)
    detalles = dict(run.details_json or {})
    ya = set(detalles.get("revertidos") or [])
    items = [
        it.before_json
        for it in (
            await session.execute(
                select(DataRepairItem).where(DataRepairItem.run_id == run_id)
            )
        ).scalars()
        if it.before_json and it.before_json.get("expense_id") not in ya
    ]
    gasto_ids = [uuid.UUID(b["expense_id"]) for b in items]
    capital_ids = [uuid.UUID(b["capital_id"]) for b in items]
    gastos = {
        g.id: g
        for g in (
            await session.execute(
                select(ExpenseEntry)
                .where(ExpenseEntry.tenant_id == tenant_id, ExpenseEntry.id.in_(gasto_ids))
                .order_by(ExpenseEntry.id)
                .with_for_update()
            )
        ).scalars()
    }
    capitales = {
        c.id: c
        for c in (
            await session.execute(
                select(OwnerCapitalMovement)
                .where(
                    OwnerCapitalMovement.tenant_id == tenant_id,
                    OwnerCapitalMovement.id.in_(capital_ids),
                )
                .order_by(OwnerCapitalMovement.id)
                .with_for_update()
            )
        ).scalars()
    }
    intactos: list[tuple[ExpenseEntry, OwnerCapitalMovement]] = []
    for antes in items:
        g = gastos.get(uuid.UUID(antes["expense_id"]))
        cap = capitales.get(uuid.UUID(antes["capital_id"]))
        motivo = _par_intacto(g, cap, antes, run.id)
        if motivo is not None:
            res.pendientes[antes["expense_id"]] = motivo
            continue
        assert g is not None and cap is not None
        intactos.append((g, cap))
    if intactos:
        dias = {_dia(g) for g, _ in intactos}
        caja_antes = await foto_de_caja(session, tenant_id, dias)
        ahora = datetime.now(UTC)
        for g, cap in intactos:
            cap.voided_at = ahora
            cap.void_reason = VOID_REASON_CAPITAL
            g.voided_at = None
            g.void_reason = None
            g.voided_by_repair_run_id = None
            res.revertidos.append(str(g.id))
        await session.flush()
        diferencias = diferencias_de_caja(
            caja_antes, await foto_de_caja(session, tenant_id, dias)
        )
        if diferencias:
            raise CajaCambioError(diferencias)

    detalles["revertidos"] = sorted(ya | set(res.revertidos))
    detalles["pendientes"] = res.pendientes
    run.details_json = detalles
    if res.completa:
        run.status = "REVERTED"
    session.add(
        DecisionAuditLog(
            tenant_id=tenant_id,
            decision_type="REPAIR_PROFIT_WITHDRAWAL_TO_CAPITAL_REVERTED",
            decision_data={
                "run_id": str(run.id),
                "revertidos": res.revertidos,
                "pendientes": res.pendientes,
                "completa": res.completa,
            },
            triggered_by=TRIGGERED_BY,
            context={"accion": "revert"},
            created_at=datetime.now(UTC),
        )
    )
    await session.flush()
    return res
