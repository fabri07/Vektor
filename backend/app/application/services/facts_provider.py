"""FactsProvider — el seam entre FactsService y el ORM real de Véktor.

Este es el ÚNICO módulo donde los nombres de campo reales (SaleEntry.amount,
ExpenseEntry.expense_type, Product.unit_cost_ars...) se mapean al contrato
canónico de columnas que exige `FactsDataProvider` (facts_service.py).
FactsService no conoce SQLAlchemy; acá no se computa NINGUNA métrica.

Patrón: "preloaded". FactsService es sync puro y computa N facts por snapshot;
los repos son async. En lugar de async-ificar el servicio, `build_facts_service`
corre las queries UNA vez (cubriendo período actual + anterior para la
variación %) y arma un `PreloadedFactsProvider` sync que sirve los DataFrames
desde memoria. Mismo patrón fetch-then-compute que forecast_service.

Mapeos clave (contratos verificados en app/persistence/models/transaction.py):
    - cost_ars / cost_confirmed por venta = el snapshot que congeló M·2a (o
      M·2c para las viejas). NUNCA el costo actual del producto: una compra
      nueva no revalúa el pasado. Sin snapshot (venta sin producto, sin costear,
      ``sin_costo``) → NaN y la venta baja la cobertura.
    - last_sold_date NO existe en Product → max(transaction_date) por product_id
      (histórico completo, sin ventana: el sobrestock necesita el máximo real).
    - soft-delete: voided_at IS NULL siempre (deactivated products fuera).
    - provenance llega crudo (REAL/DEMO); el filtro lo hace FactsService.
    - payment_method="inflow" (cobros de fiado) SE INCLUYE: FactsService decide.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.facts_service import (
    BusinessFact,
    FactsService,
    Period,
    SeverityThresholds,
)
from app.config.avg_cost_display_rollout import avg_cost_display_enabled_for
from app.domain.business_time import AR_TZ
from app.domain.verticals import Vertical, try_parse_vertical
from app.observability.logger import get_logger
from app.persistence.models.business import BusinessProfile
from app.persistence.models.owner_capital import OwnerCapitalMovement
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.persistence.repositories._expense_scope import gasto_de_resultado
from app.persistence.repositories._pos_tender_scope import tenders_mixtos

logger = get_logger(__name__)


def _naive_ar(value: datetime | None) -> datetime | None:
    """Normaliza a datetime naive en hora AR (created_at es tz-aware UTC;
    acquired_at/transaction_date son naive — mezclar rompe pd.to_datetime)."""
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(AR_TZ).replace(tzinfo=None)

_SALES_COLS = [
    "sale_date", "amount_ars", "cost_ars", "cost_confirmed", "cost_avg_ars", "cost_avg_estado",
    "payment_method", "provenance",
]
_MIXED_TENDERS_COLS = ["sale_date", "amount_ars", "payment_method", "provenance"]
_EXPENSES_COLS = ["expense_date", "amount_ars", "category", "expense_type", "provenance"]
_PRODUCTS_COLS = [
    "product_id", "stock_units", "base_units_per_sale_unit", "unit_cost_ars", "last_sold_date",
    "first_seen_date", "provenance",
]

_CAPITAL_COLS = ["occurred_on", "kind", "form", "amount_ars", "partner_name"]


@dataclass(frozen=True)
class FactsFrames:
    """DataFrames normalizados + tenant y rango que cubren (guards anti mal uso)."""

    tenant_id: str
    sales: pd.DataFrame
    expenses: pd.DataFrame
    products: pd.DataFrame
    mixed_tenders: pd.DataFrame
    coverage_start: date
    coverage_end: date


async def _gastos(
    session: AsyncSession, tenant_id: uuid.UUID, start: date, end: date
) -> pd.DataFrame:
    # ── Gastos (expense_type = frontera canónica COGS/OPEX) ─────────────────
    expenses_q = select(
        ExpenseEntry.transaction_date,
        ExpenseEntry.amount,
        ExpenseEntry.category,
        ExpenseEntry.expense_type,
        ExpenseEntry.provenance,
    ).where(
        ExpenseEntry.tenant_id == tenant_id,
        ExpenseEntry.voided_at.is_(None),
        func.date(ExpenseEntry.transaction_date) >= start,
        func.date(ExpenseEntry.transaction_date) <= end,
        # Alimenta margen neto y gastos por categoría, que conviven con el
        # valor del stock: el flete capitalizado no se cuenta dos veces.
        gasto_de_resultado(ExpenseEntry.custom_fields),
    )
    expense_rows = (await session.execute(expenses_q)).all()
    return pd.DataFrame(
        [
            {
                "expense_date": row.transaction_date,
                "amount_ars": float(row.amount),
                "category": row.category,
                "expense_type": row.expense_type,
                "provenance": row.provenance,
            }
            for row in expense_rows
        ],
        columns=_EXPENSES_COLS,
    )


async def load_facts_frames(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    start: date,
    end: date,
    completo: bool = True,
) -> FactsFrames:
    """Corre las queries bulk (sin limit) y arma los DataFrames canónicos.

    ``completo=False`` trae sólo ventas y gastos (lo que necesitan los márgenes):
    sin tenders mixtos ni productos, que incluyen un ``max(transaction_date)``
    sobre TODA la historia de ventas. Lo usa ``margenes_del_tenant`` en cada
    turno del chat.
    """
    # ── Ventas + snapshot de costo congelado (M·2a/M·2c) ────────────────────
    sales_q = select(
        SaleEntry.transaction_date,
        SaleEntry.amount,
        SaleEntry.cost_ars,
        SaleEntry.cost_confirmed,
        SaleEntry.cost_avg_ars,
        SaleEntry.cost_avg_estado,
        SaleEntry.payment_method,
        SaleEntry.provenance,
    ).where(
        SaleEntry.tenant_id == tenant_id,
        SaleEntry.voided_at.is_(None),
        func.date(SaleEntry.transaction_date) >= start,
        func.date(SaleEntry.transaction_date) <= end,
    )
    sales_rows = (await session.execute(sales_q)).all()
    sales_df = pd.DataFrame(
        [
            {
                "sale_date": row.transaction_date,
                "amount_ars": float(row.amount),
                "cost_ars": float(row.cost_ars) if row.cost_ars is not None else float("nan"),
                # Sólo True confirma; NULL (sin snapshot) no.
                "cost_confirmed": row.cost_confirmed is True,
                # M·P3: el costo promedio de la venta (dato, no el costo principal).
                "cost_avg_ars": (
                    float(row.cost_avg_ars) if row.cost_avg_ars is not None else float("nan")
                ),
                "cost_avg_estado": row.cost_avg_estado,
                "payment_method": row.payment_method,
                "provenance": row.provenance,
            }
            for row in sales_rows
        ],
        columns=_SALES_COLS,
    )

    if not completo:
        return FactsFrames(
            tenant_id=str(tenant_id),
            sales=sales_df,
            expenses=await _gastos(session, tenant_id, start, end),
            products=pd.DataFrame(columns=_PRODUCTS_COLS),
            mixed_tenders=pd.DataFrame(columns=_MIXED_TENDERS_COLS),
            coverage_start=start,
            coverage_end=end,
        )

    # ── Tenders de las operaciones de caja mixtas (B9) ──────────────────────
    # Una fila por tender; la procedencia es la de las líneas de la operación,
    # así que FactsService les aplica el MISMO filtro que a las ventas.
    mixtos = tenders_mixtos(tenant_id, start, end)
    mixed_rows = (await session.execute(select(mixtos))).all()
    mixed_df = pd.DataFrame(
        [
            {
                "sale_date": row.sale_date,
                "amount_ars": float(row.amount_ars),
                "payment_method": row.payment_method,
                "provenance": row.provenance,
            }
            for row in mixed_rows
        ],
        columns=_MIXED_TENDERS_COLS,
    )

    expenses_df = await _gastos(session, tenant_id, start, end)

    # ── Productos activos + last_sold_date derivado (histórico completo) ────
    products_q = select(
        Product.id,
        Product.stock_units,
        Product.unit_cost_ars,
        Product.base_units_per_sale_unit,
        Product.acquired_at,
        Product.created_at,
        Product.provenance,
    ).where(
        Product.tenant_id == tenant_id,
        Product.is_active.is_(True),
        Product.deactivated_at.is_(None),
    )
    product_rows = (await session.execute(products_q)).all()

    last_sold_q = (
        select(
            SaleEntry.product_id,
            func.max(SaleEntry.transaction_date).label("last_sold"),
        )
        .where(
            SaleEntry.tenant_id == tenant_id,
            SaleEntry.voided_at.is_(None),
            SaleEntry.product_id.isnot(None),
        )
        .group_by(SaleEntry.product_id)
    )
    last_sold_rows = (await session.execute(last_sold_q)).all()
    last_sold_map = {str(row.product_id): row.last_sold for row in last_sold_rows}

    products_df = pd.DataFrame(
        [
            {
                "product_id": str(row.id),
                "stock_units": float(row.stock_units),
                # Para valuar: el costo es por unidad de venta (ver sale_unit.valor_de_stock).
                "base_units_per_sale_unit": int(row.base_units_per_sale_unit or 1),
                "unit_cost_ars": (
                    float(row.unit_cost_ars) if row.unit_cost_ars is not None
                    else float("nan")
                ),
                "last_sold_date": last_sold_map.get(str(row.id), pd.NaT),
                # ancla de antigüedad para sobrestock de nunca-vendidos
                "first_seen_date": _naive_ar(row.acquired_at or row.created_at),
                "provenance": row.provenance,
            }
            for row in product_rows
        ],
        columns=_PRODUCTS_COLS,
    )

    return FactsFrames(
        tenant_id=str(tenant_id),
        sales=sales_df,
        expenses=expenses_df,
        products=products_df,
        mixed_tenders=mixed_df,
        coverage_start=start,
        coverage_end=end,
    )


async def load_capital_frame(
    session: AsyncSession, tenant_id: uuid.UUID, hasta: date
) -> pd.DataFrame:
    """Movimientos de capital VIVOS del tenant con fecha de negocio <= ``hasta`` (D·4).

    Historia completa, sin piso: el préstamo pendiente es acumulado. Lo consume
    ``facts_service.capital_de_socios``; no va en ``load_facts_frames`` porque el
    capital no es parte del snapshot del dashboard.
    """
    m = OwnerCapitalMovement
    rows = (
        await session.execute(
            select(m.occurred_on, m.kind, m.form, m.amount_ars, m.partner_name).where(
                m.tenant_id == tenant_id,
                m.voided_at.is_(None),
                m.occurred_on <= hasta,
            )
        )
    ).all()
    return pd.DataFrame(
        [
            {
                "occurred_on": row.occurred_on,
                "kind": row.kind,
                "form": row.form,
                "amount_ars": float(row.amount_ars),
                "partner_name": row.partner_name,
            }
            for row in rows
        ],
        columns=_CAPITAL_COLS,
    )


class PreloadedFactsProvider:
    """Implementa el Protocol `FactsDataProvider` (sync) sirviendo frames precargados.

    Dos guards, misma filosofía que la no-invention rule (fallar explícito antes
    que responder con data equivocada en silencio):
    - rango fuera del precargado → ValueError;
    - tenant distinto al de los frames → ValueError (un provider precargado es
      de UN tenant; servir los números de otro sería una fuga silenciosa).
    """

    def __init__(self, frames: FactsFrames) -> None:
        self._frames = frames

    def _check_tenant(self, tenant_id: str) -> None:
        if str(tenant_id) != self._frames.tenant_id:
            raise ValueError(
                f"Provider precargado para tenant {self._frames.tenant_id}; "
                f"se pidió {tenant_id}"
            )

    def _check_coverage(self, start: date, end: date) -> None:
        if start < self._frames.coverage_start or end > self._frames.coverage_end:
            raise ValueError(
                f"Rango [{start}, {end}] fuera de la cobertura precargada "
                f"[{self._frames.coverage_start}, {self._frames.coverage_end}]"
            )

    @staticmethod
    def _slice(df: pd.DataFrame, date_col: str, start: date, end: date) -> pd.DataFrame:
        if df.empty:
            return df.copy()
        d = pd.to_datetime(df[date_col]).dt.date
        return df[(d >= start) & (d <= end)].copy()

    def sales(self, tenant_id: str, start: date, end: date) -> pd.DataFrame:
        self._check_tenant(tenant_id)
        self._check_coverage(start, end)
        return self._slice(self._frames.sales, "sale_date", start, end)

    def expenses(self, tenant_id: str, start: date, end: date) -> pd.DataFrame:
        self._check_tenant(tenant_id)
        self._check_coverage(start, end)
        return self._slice(self._frames.expenses, "expense_date", start, end)

    def products(self, tenant_id: str) -> pd.DataFrame:
        self._check_tenant(tenant_id)
        return self._frames.products.copy()

    def mixed_tenders(self, tenant_id: str, start: date, end: date) -> pd.DataFrame:
        self._check_tenant(tenant_id)
        self._check_coverage(start, end)
        return self._slice(self._frames.mixed_tenders, "sale_date", start, end)


def thresholds_for_vertical(vertical: Vertical) -> SeverityThresholds:
    """Umbrales de severidad desde el benchmark canónico del vertical.

    MISMA fuente JSON que usa el health engine (heuristics/verticals/loader.py),
    convertida de fracciones a % — así el severity del fact y el health score
    no pueden contradecirse. Sin fallback: el vertical llega tipado.
    """
    from app.heuristics.verticals.loader import load_margin_benchmark  # noqa: PLC0415

    margin = load_margin_benchmark(vertical)
    return SeverityThresholds(
        margen_neto_floor=float(margin.warning_below) * 100.0,
        margen_neto_critical=float(margin.critical_below) * 100.0,
    )


async def build_facts_service(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    period: Period,
    *,
    vertical: Vertical | None = None,
    thresholds: SeverityThresholds | None = None,
) -> FactsService:
    """Carga [período_anterior.start, período.end] en UNA pasada y arma el servicio.

    La cobertura incluye el período previo porque ventas_periodo/ticket_promedio
    computan la variación % contra él. Los umbrales de severidad salen del
    benchmark JSON de `vertical`, salvo que se pasen `thresholds` explícitos (que
    tienen precedencia).

    Exige al menos uno de los dos: construir el servicio sin umbrales deja
    `margen_neto` sin `severity` y apaga en silencio las alertas de margen río
    abajo. Si un caller legítimamente no tiene vertical, tiene que declarar sus
    umbrales — no omitir ambos.
    """
    if vertical is None and thresholds is None:
        raise ValueError(
            "build_facts_service requiere `vertical` o `thresholds` explícitos: "
            "sin umbrales, margen_neto queda sin severity."
        )
    if thresholds is None and vertical is not None:
        thresholds = thresholds_for_vertical(vertical)
    frames = await load_facts_frames(
        session,
        tenant_id,
        start=period.previous().start,
        end=period.end,
    )
    return FactsService(
        PreloadedFactsProvider(frames),
        thresholds,
        fiscal_condition_crudo=await _regimen_crudo(session, tenant_id),
    )


async def _regimen_crudo(session: AsyncSession, tenant_id: uuid.UUID) -> str | None:
    """CRUDO: el margen de un RI compara sin IVA y el legacy «registered» no
    alcanza para saberlo (ver domain/margen.py y base_comparable)."""
    return (
        await session.execute(
            select(BusinessProfile.fiscal_condition)
            .where(BusinessProfile.tenant_id == tenant_id)
            .limit(1)
        )
    ).scalar_one_or_none()


@dataclass(frozen=True)
class MargenesDelPeriodo:
    """Los dos márgenes del negocio, del MISMO FactsService (M·2b-2).

    ``bruto`` = ventas − costo vendido (definición única, ``domain/margen.py``);
    ``neto`` = ventas − TODOS los gastos (el resultado operativo).
    """

    bruto: BusinessFact
    neto: BusinessFact
    #: M·P4-2: el margen con costo promedio al lado del de reposición. ``None``
    #: si la cuenta no está habilitada (``AVG_COST_DISPLAY_ROLLOUT_TENANT_IDS``):
    #: nadie lo ve hasta que el dueño revise su sombra. Nunca es un 2.º margen.
    promedio_vs_reposicion: BusinessFact | None = None


async def margenes_del_tenant(
    session: AsyncSession, tenant_id: uuid.UUID, *, dias: int = 30
) -> MargenesDelPeriodo | None:
    """Margen bruto y neto de los últimos ``dias`` para el chat.

    Arma el servicio con el rubro del perfil y ``include_demo`` según el tenant
    (en un tenant demo todo es ``provenance='DEMO'``; un tenant real no ve filas
    demo), igual que la explicación de alertas. Sin perfil → ``None``: no hay
    rubro contra el que medir y el llamador dice que faltan datos.
    """
    vertical_code = (
        await session.execute(
            select(BusinessProfile.vertical_code)
            .where(BusinessProfile.tenant_id == tenant_id)
            .limit(1)
        )
    ).scalar_one_or_none()
    vertical = try_parse_vertical(vertical_code)
    if vertical is None:
        return None
    tenant = await session.get(Tenant, tenant_id)
    include_demo = bool(tenant and tenant.is_demo)
    period = Period.last_n_days(dias)
    # Sólo ventas y gastos del período: los márgenes no comparan contra el
    # anterior ni miran productos (corre en cada turno del chat).
    frames = await load_facts_frames(
        session, tenant_id, start=period.start, end=period.end, completo=False
    )
    svc = FactsService(
        PreloadedFactsProvider(frames),
        thresholds_for_vertical(vertical),
        fiscal_condition_crudo=await _regimen_crudo(session, tenant_id),
    )
    tid = str(tenant_id)
    return MargenesDelPeriodo(
        bruto=svc.margen_bruto(tid, period, include_demo=include_demo),
        neto=svc.margen_neto(tid, period, include_demo=include_demo),
        promedio_vs_reposicion=_promedio_vs_reposicion(svc, tid, period, include_demo),
    )


def _promedio_vs_reposicion(
    svc: FactsService, tid: str, period: Period, include_demo: bool
) -> BusinessFact | None:
    """El dato de M·P4-2, o ``None`` sin compuerta o si falla.

    Va aparte para que un error suyo nunca tumbe bruto y neto: ``leer_margenes``
    descartaría los tres, y el score se quedaría sin margen.
    """
    if not avg_cost_display_enabled_for(tid):
        return None
    try:
        return svc.margen_promedio_vs_reposicion(tid, period, include_demo=include_demo)
    except Exception as exc:  # noqa: BLE001 — es un dato accesorio al margen
        logger.warning("margen_promedio_vs_reposicion_fallo", tenant_id=tid, error=str(exc))
        return None


MOTIVO_SIN_RUBRO = "falta el rubro del negocio"
MOTIVO_ERROR = "no se pudo medir ahora"


@dataclass(frozen=True)
class LecturaDeMargenes:
    """``margenes_del_tenant`` sin que un error tumbe la respuesta del chat.

    ``margenes`` es ``None`` sin perfil o si falló, con ``motivo`` para decirlo.
    """

    margenes: MargenesDelPeriodo | None
    motivo: str | None = None


async def leer_margenes(session: AsyncSession, tenant_id: uuid.UUID) -> LecturaDeMargenes:
    """Lee los márgenes dentro de un SAVEPOINT y nunca levanta.

    Antes el chat leía el margen de ``get_financial_summary``, que atrapaba sus
    errores; sin esto, un fallo de FactsService convertía la consulta de caja o
    el punto de equilibrio en «Error al ejecutar la tarea». El savepoint evita
    que, en Postgres, un error de base deje abortada la transacción del pedido
    (memoria de sesión, auditoría).
    """
    try:
        async with session.begin_nested():
            margenes = await margenes_del_tenant(session, tenant_id)
    except Exception as exc:  # noqa: BLE001 — el margen es accesorio a la respuesta
        logger.warning("margenes_del_tenant_fallo", tenant_id=str(tenant_id), error=str(exc))
        return LecturaDeMargenes(None, MOTIVO_ERROR)
    if margenes is None:
        return LecturaDeMargenes(None, MOTIVO_SIN_RUBRO)
    return LecturaDeMargenes(margenes)
