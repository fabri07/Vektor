"""
Business State Service.

Computes a rich, denormalized BusinessState for a tenant by combining:
  - Real transaction data (last 30 days) — preferred source.
  - Onboarding estimates from BusinessProfile — fallback when real data is absent.

This state is the ONLY input to the Health Engine; no raw DB data ever
reaches score computation directly.

Cache strategy
--------------
  Redis key  : business_state:v4:{tenant_id}     — JSON blob, TTL 24 h
  Redis key  : last_inputs_hash:v4:{tenant_id}   — SHA-256 of input fingerprint, TTL 24 h

  El prefijo de versión (`v4:`) se bumpea cuando cambia el FORMATO del blob **o el
  SIGNIFICADO de alguno de sus campos**, para no leer con el código nuevo lo que
  escribió el viejo. El segundo caso es el más traicionero: el blob deserializa
  sin error y sirve números calculados con otras reglas. Ver `_cache_key`.

On every call:
  1. Compute inputs fingerprint (sale_count, expense_count, product_count, profile.updated_at).
  2. If cached state exists AND fingerprint matches → return deserialized cached state.
  3. Otherwise recompute, persist snapshot, update both Redis keys, return.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.verticals import try_parse_vertical
from app.observability.logger import get_logger
from app.persistence.models.business import BusinessSnapshot
from app.persistence.models.cash_close import CashClose
from app.persistence.models.product import Product
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.persistence.repositories.business_profile_repository import BusinessProfileRepository

logger = get_logger(__name__)

# Métodos de pago "líquidos" = plata disponible hoy. Excluye 'account' (fiado/
# cuenta corriente, no realizado), 'credit_card' (acreditación diferida ~30d) y
# 'other' (desconocido). Definición de caja para el health score.
_LIQUID_METHODS = ("cash", "transfer", "qr", "debit_card")

CACHE_TTL_SECONDS = 60 * 60 * 24  # 24 h


# ── Dataclasses ────────────────────────────────────────────────────────────────


@dataclass
class ProductSummary:
    product_id: UUID
    name: str
    stock_units: int
    # None = no configurado por el tenant; se interpreta con effective_threshold()
    low_stock_threshold_units: int | None
    sale_price_ars: Decimal
    units_sold_30d: int | None = None


@dataclass
class BusinessState:
    """
    Enriched business state ready for the Health Engine.
    All monetary values are in ARS.
    """

    snapshot_id: UUID
    tenant_id: UUID
    vertical_code: str
    data_completeness_score: float
    confidence_level: str  # HIGH | MEDIUM | LOW
    monthly_sales_est: Decimal
    monthly_inventory_cost_est: Decimal
    monthly_fixed_expenses_est: Decimal
    cash_on_hand_est: Decimal
    product_count: int
    supplier_count: int
    products: list[ProductSummary]
    main_concern: str | None
    # Ventas reales del período anterior (días 31-60). Decimal("0") si no hay historial.
    prev_monthly_sales_est: Decimal = Decimal("0")
    # Fuente de la caja: "arqueo" | "onboarding" | "flujo" | "desconocido".
    # Define cómo el health engine puntúa la dimensión caja (saldo vs cobertura).
    cash_source: str = "desconocido"
    # Flujos líquidos en la ventana (cash+transfer+qr+debit). Usados en modo "flujo".
    liquid_inflow_est: Decimal = Decimal("0")
    liquid_outflow_est: Decimal = Decimal("0")


# ── Cache helpers ──────────────────────────────────────────────────────────────


# v4: `data_completeness_score` cambió de SIGNIFICADO — ahora pondera por
# procedencia (observado vs declarado). El blob v3 se deserializa sin error, que
# es lo que lo hace peor que un cambio de formato: el fingerprint no depende del
# código, así que un tenant sin movimientos nuevos seguiría sirviendo su
# completeness vieja —y su confianza vieja— durante las 24 h de
# CACHE_TTL_SECONDS posteriores al deploy, sin ninguna señal de que está
# mostrando un número calculado con otras reglas.
#
# (v3: se eliminó el campo `ruleset` del estado, y con él la clave `"ruleset"`
# del blob donde vivía escondido el vertical; un blob v2 moría con KeyError en
# `_deserialize_state`. v2: los rulesets pasaron del código corto "kiosco" al
# canónico "kiosco_almacen".)
def _cache_key(tenant_id: UUID) -> str:
    return f"business_state:v4:{tenant_id}"


def _hash_key(tenant_id: UUID) -> str:
    return f"last_inputs_hash:v4:{tenant_id}"


def _make_fingerprint(
    sale_count: int,
    expense_count: int,
    product_count: int,
    profile_updated_at: datetime,
    prev_sales_amount: str = "0",
) -> str:
    raw = (
        f"{sale_count}:{expense_count}:{product_count}:"
        f"{profile_updated_at.isoformat()}:{prev_sales_amount}"
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _serialize_state(state: BusinessState) -> str:
    """JSON-serialize BusinessState for Redis storage."""
    d = asdict(state)
    # Convert non-serializable types
    d["snapshot_id"] = str(state.snapshot_id)
    d["tenant_id"] = str(state.tenant_id)
    d["monthly_sales_est"] = str(state.monthly_sales_est)
    d["monthly_inventory_cost_est"] = str(state.monthly_inventory_cost_est)
    d["monthly_fixed_expenses_est"] = str(state.monthly_fixed_expenses_est)
    d["cash_on_hand_est"] = str(state.cash_on_hand_est)
    d["prev_monthly_sales_est"] = str(state.prev_monthly_sales_est)
    d["liquid_inflow_est"] = str(state.liquid_inflow_est)
    d["liquid_outflow_est"] = str(state.liquid_outflow_est)
    # `vertical_code` NO necesita línea propia: es un campo del dataclass y
    # `asdict()` ya lo trae. La asignación explícita que había acá era herencia
    # del patrón viejo, donde el vertical viajaba dentro de `ruleset` porque el
    # ruleset no era serializable.
    d["products"] = [
        {
            "product_id": str(p.product_id),
            "name": p.name,
            "stock_units": p.stock_units,
            "low_stock_threshold_units": p.low_stock_threshold_units,
            "sale_price_ars": str(p.sale_price_ars),
            "units_sold_30d": p.units_sold_30d,
        }
        for p in state.products
    ]
    return json.dumps(d)


def _deserialize_state(raw: str) -> BusinessState:
    """Restore BusinessState from JSON string."""
    d = json.loads(raw)
    vertical_code: str = d["vertical_code"]
    if try_parse_vertical(vertical_code) is None:
        raise ValueError(f"Unknown vertical in cached state: {vertical_code!r}")
    products = [
        ProductSummary(
            product_id=UUID(p["product_id"]),
            name=p["name"],
            stock_units=p["stock_units"],
            low_stock_threshold_units=p.get("low_stock_threshold_units"),  # None = usar default
            sale_price_ars=Decimal(p["sale_price_ars"]),
            units_sold_30d=p.get("units_sold_30d"),
        )
        for p in d["products"]
    ]
    return BusinessState(
        snapshot_id=UUID(d["snapshot_id"]),
        tenant_id=UUID(d["tenant_id"]),
        vertical_code=d["vertical_code"],
        data_completeness_score=float(d["data_completeness_score"]),
        confidence_level=d["confidence_level"],
        monthly_sales_est=Decimal(d["monthly_sales_est"]),
        monthly_inventory_cost_est=Decimal(d["monthly_inventory_cost_est"]),
        monthly_fixed_expenses_est=Decimal(d["monthly_fixed_expenses_est"]),
        cash_on_hand_est=Decimal(d["cash_on_hand_est"]),
        product_count=d["product_count"],
        supplier_count=d["supplier_count"],
        products=products,
        main_concern=d.get("main_concern"),
        prev_monthly_sales_est=Decimal(d.get("prev_monthly_sales_est", "0")),
        cash_source=d.get("cash_source", "desconocido"),
        liquid_inflow_est=Decimal(d.get("liquid_inflow_est", "0")),
        liquid_outflow_est=Decimal(d.get("liquid_outflow_est", "0")),
    )


# ── Completeness scoring ───────────────────────────────────────────────────────


# Cuánto vale un input que sólo está DECLARADO (el dueño lo contestó en el
# onboarding) frente al mismo input OBSERVADO (respaldado por transacciones,
# arqueos o maestros cargados).
#
# El 0.4 no es arbitrario: con los seis inputs declarados y ninguno observado
# el techo es 40 — por debajo de los 50 que la no-invention rule exige para
# mostrar análisis. Es la propiedad que hace falta y está cubierta por un test.
# Contestar un formulario no puede, por sí solo, habilitar conclusiones sobre
# un negocio del que no se registró ni un movimiento.
PESO_DECLARADO = 0.4


def _compute_completeness(
    has_sales: bool,
    has_inventory_cost: bool,
    has_fixed_expenses: bool,
    has_cash_on_hand: bool,
    product_count: int,
    supplier_count: int,
    *,
    sales_observado: bool = False,
    inventory_cost_observado: bool = False,
    fixed_expenses_observado: bool = False,
    cash_observado: bool = False,
    products_observado: bool = False,
    suppliers_observado: bool = False,
) -> float:
    """Completeness ponderada por PROCEDENCIA del dato, no sólo por presencia.

    Antes contaba `has_*`, que se calculan sobre los `*_est` — y esos caen a
    las estimaciones del onboarding cuando no hay datos reales. El resultado
    era que la métrica medía "¿contestó la encuesta?" en vez de "¿tengo
    datos?", y era justo la métrica que la no-invention rule usa de compuerta.
    """

    def _puntos(presente: bool, observado: bool, peso: float) -> float:
        if not presente:
            return 0.0
        return peso if observado else peso * PESO_DECLARADO

    return (
        _puntos(has_sales, sales_observado, 25.0)
        + _puntos(has_inventory_cost, inventory_cost_observado, 20.0)
        + _puntos(has_fixed_expenses, fixed_expenses_observado, 15.0)
        + _puntos(has_cash_on_hand, cash_observado, 20.0)
        + _puntos(product_count >= 5, products_observado, 10.0)
        + _puntos(supplier_count >= 1, suppliers_observado, 10.0)
    )


def _derive_confidence(score: float) -> str:
    if score >= 80.0:
        return "HIGH"
    if score >= 50.0:
        return "MEDIUM"
    return "LOW"


def _estimado_o_cero(real: Decimal, estimado_del_perfil: Decimal | None) -> Decimal:
    """Costo/gasto mensual: lo real si existe, si no el estimado del onboarding.

    **Hoy esto no cambia ningún número.** Reemplaza a
    ``estimado_del_perfil or Decimal("0")``, y como las dos ramas devuelven
    ``Decimal("0")`` el resultado es idéntico. Está escrito así para que la
    distinción entre "no contestó" y "contestó cero" quede explícita en el punto
    donde importa, y para que el día que alguien quiera usarla no tenga que
    descubrir primero que un ``or`` la estaba borrando.

    Y sobre por qué la lectura NO la usa todavía: ``has_inventory_cost`` y
    ``has_fixed_expenses`` siguen midiendo ``> 0``, así que un cero declarado no
    suma completeness. Es deliberado. Los ceros ya guardados en la base son
    ambiguos —el formulario viejo convertía cada campo en blanco en un cero— y
    no hay forma de distinguir cuáles fueron una respuesta real. Contarlos como
    dato presente le daría puntos justamente a las filas que fabricó el bug. La
    ambigüedad se drena sola: desde ahora un campo en blanco se guarda NULL.
    """
    if real > 0:
        return real
    if estimado_del_perfil is not None:
        return estimado_del_perfil
    return Decimal("0")


def _derive_main_concern(
    score: float,
    has_sales: bool,
    has_inventory_cost: bool,
    has_fixed_expenses: bool,
) -> str | None:
    if score < 50.0:
        return "Datos insuficientes para un análisis confiable"
    if not has_sales:
        return "Sin datos de ventas reales"
    if not has_inventory_cost:
        return "Sin datos de costo de mercadería"
    if not has_fixed_expenses:
        return "Sin datos de gastos fijos reales"
    return None


# ── Main function ──────────────────────────────────────────────────────────────


async def compute_business_state(
    tenant_id: UUID,
    session: AsyncSession,
    redis: Redis,
) -> BusinessState:
    """
    Compute (or return cached) BusinessState for the given tenant.

    Raises
    ------
    ValueError
        If no BusinessProfile exists for the tenant or the vertical is unknown.
    """
    now = datetime.now(UTC)
    window_start = (now - timedelta(days=30)).date()
    window_end = now.date()

    # ── 1. Load BusinessProfile ───────────────────────────────────────────────
    bp_repo = BusinessProfileRepository(session)
    profile = await bp_repo.get_by_tenant_id(tenant_id)
    if profile is None:
        raise ValueError(f"No BusinessProfile found for tenant {tenant_id}")

    if try_parse_vertical(profile.vertical_code) is None:
        raise ValueError(f"Unknown vertical_code: {profile.vertical_code!r}")

    # ── 2. Aggregate sales (last 30 days) ────────────────────────────────────
    sale_sum_result = await session.execute(
        select(
            func.sum(SaleEntry.amount),
            func.count(SaleEntry.id),
            func.min(SaleEntry.transaction_date),
            func.max(SaleEntry.transaction_date),
        ).where(
            SaleEntry.tenant_id == tenant_id,
            SaleEntry.voided_at.is_(None),
            SaleEntry.transaction_date >= window_start,
            func.date(SaleEntry.transaction_date) <= window_end,
        )
    )
    sale_sum_row = sale_sum_result.one()
    real_sales: Decimal = Decimal(str(sale_sum_row[0] or 0))
    sale_count: int = int(sale_sum_row[1] or 0)
    first_sale_date = sale_sum_row[2]
    last_sale_date = sale_sum_row[3]

    # ── 2b. Previous 30-day sales (days 31–60 ago) — para score de crecimiento ─
    prev_window_start = (now - timedelta(days=60)).date()
    prev_window_end = (now - timedelta(days=31)).date()
    prev_sale_result = await session.execute(
        select(func.sum(SaleEntry.amount)).where(
            SaleEntry.tenant_id == tenant_id,
            SaleEntry.voided_at.is_(None),
            SaleEntry.transaction_date >= prev_window_start,
            func.date(SaleEntry.transaction_date) <= prev_window_end,
        )
    )
    prev_real_sales: Decimal = Decimal(str(prev_sale_result.scalar_one() or 0))

    # ── 3. Aggregate expenses (last 30 days) ─────────────────────────────────
    # mercaderia cost = expenses categorized as 'mercaderia'
    inv_sum_result = await session.execute(
        select(func.sum(ExpenseEntry.amount), func.count(ExpenseEntry.id)).where(
            ExpenseEntry.tenant_id == tenant_id,
            ExpenseEntry.voided_at.is_(None),
            ExpenseEntry.transaction_date >= window_start,
            func.date(ExpenseEntry.transaction_date) <= window_end,
            ExpenseEntry.category == "mercaderia",
        )
    )
    inv_row = inv_sum_result.one()
    real_inventory_cost: Decimal = Decimal(str(inv_row[0] or 0))

    # fixed expenses = is_recurring = True
    fixed_sum_result = await session.execute(
        select(func.sum(ExpenseEntry.amount), func.count(ExpenseEntry.id)).where(
            ExpenseEntry.tenant_id == tenant_id,
            ExpenseEntry.voided_at.is_(None),
            ExpenseEntry.transaction_date >= window_start,
            func.date(ExpenseEntry.transaction_date) <= window_end,
            ExpenseEntry.is_recurring.is_(True),
        )
    )
    fixed_row = fixed_sum_result.one()
    real_fixed_expenses: Decimal = Decimal(str(fixed_row[0] or 0))

    # ── 3b. Flujos líquidos en la ventana (para el modo cobertura de caja) ───────
    liquid_in_result = await session.execute(
        select(func.sum(SaleEntry.amount)).where(
            SaleEntry.tenant_id == tenant_id,
            SaleEntry.voided_at.is_(None),
            SaleEntry.transaction_date >= window_start,
            func.date(SaleEntry.transaction_date) <= window_end,
            SaleEntry.payment_method.in_(_LIQUID_METHODS),
        )
    )
    liquid_inflow_est: Decimal = Decimal(str(liquid_in_result.scalar_one() or 0))
    liquid_out_result = await session.execute(
        select(func.sum(ExpenseEntry.amount)).where(
            ExpenseEntry.tenant_id == tenant_id,
            ExpenseEntry.voided_at.is_(None),
            ExpenseEntry.transaction_date >= window_start,
            func.date(ExpenseEntry.transaction_date) <= window_end,
            ExpenseEntry.payment_method.in_(_LIQUID_METHODS),
        )
    )
    liquid_outflow_est: Decimal = Decimal(str(liquid_out_result.scalar_one() or 0))

    # ── 3c. Último arqueo (CashClose) — saldo medido si existe ──────────────────
    cash_close_result = await session.execute(
        select(CashClose.counted_total_ars)
        .where(CashClose.tenant_id == tenant_id)
        .order_by(CashClose.close_date.desc())
        .limit(1)
    )
    latest_counted_cash = cash_close_result.scalar_one_or_none()

    # total expense count (for fingerprint)
    expense_count_result = await session.execute(
        select(func.count(ExpenseEntry.id)).where(
            ExpenseEntry.tenant_id == tenant_id,
            ExpenseEntry.voided_at.is_(None),
            ExpenseEntry.transaction_date >= window_start,
            func.date(ExpenseEntry.transaction_date) <= window_end,
        )
    )
    expense_count: int = int(expense_count_result.scalar_one() or 0)

    # unique supplier names
    supplier_result = await session.execute(
        select(func.count(ExpenseEntry.supplier_name.distinct())).where(
            ExpenseEntry.tenant_id == tenant_id,
            ExpenseEntry.voided_at.is_(None),
            ExpenseEntry.supplier_name.isnot(None),
        )
    )
    real_supplier_count: int = int(supplier_result.scalar_one() or 0)

    # ── 4. Active products ───────────────────────────────────────────────────
    product_result = await session.execute(
        select(Product).where(
            Product.tenant_id == tenant_id,
            Product.is_active.is_(True),
        )
    )
    active_products: list[Product] = list(product_result.scalars().all())
    real_product_count = len(active_products)

    sold_units_by_product: dict[UUID, int] = {}
    if active_products:
        sold_units_result = await session.execute(
            select(SaleEntry.product_id, func.coalesce(func.sum(SaleEntry.quantity), 0))
            .where(
                SaleEntry.tenant_id == tenant_id,
                SaleEntry.voided_at.is_(None),
                SaleEntry.transaction_date >= window_start,
                func.date(SaleEntry.transaction_date) <= window_end,
                SaleEntry.product_id.isnot(None),
            )
            .group_by(SaleEntry.product_id)
        )
        sold_units_by_product = {
            product_id: int(total or 0)
            for product_id, total in sold_units_result.all()
            if product_id is not None
        }

    # ── 5. Cache fingerprint check ───────────────────────────────────────────
    fingerprint = _make_fingerprint(
        sale_count=sale_count,
        expense_count=expense_count,
        product_count=real_product_count,
        profile_updated_at=profile.updated_at,
        prev_sales_amount=str(prev_real_sales),
    )
    cached_hash: str | None = await redis.get(_hash_key(tenant_id))
    if cached_hash == fingerprint:
        cached_blob: str | None = await redis.get(_cache_key(tenant_id))
        if cached_blob:
            logger.info("bsl.cache_hit", tenant_id=str(tenant_id))
            return _deserialize_state(cached_blob)

    # ── 6. Resolve estimates (real data preferred, fallback to onboarding) ───
    # Normalize to UTC-aware before comparing (SQLite returns naive datetimes).
    _profile_updated_at = profile.updated_at
    if _profile_updated_at.tzinfo is None:
        _profile_updated_at = _profile_updated_at.replace(tzinfo=UTC)
    onboarding_recent = profile.onboarding_completed and _profile_updated_at >= datetime.now(
        UTC
    ) - timedelta(days=7)

    onboarding_sales_est = profile.monthly_sales_estimate_ars or Decimal("0")
    if sale_count == 0:
        monthly_sales_est = onboarding_sales_est
    elif sale_count < 50:
        days_with_data = (
            (last_sale_date - first_sale_date).days + 1
            if first_sale_date is not None and last_sale_date is not None
            else 1
        )
        projected_sales = real_sales * Decimal("30") / Decimal(max(days_with_data, 1))
        if sale_count < 10:
            monthly_sales_est = onboarding_sales_est * Decimal("0.7") + projected_sales * Decimal(
                "0.3"
            )
        else:
            monthly_sales_est = onboarding_sales_est * Decimal("0.3") + projected_sales * Decimal(
                "0.7"
            )
    else:
        monthly_sales_est = real_sales
    # `or Decimal("0")` acá volvería a fabricar el cero que el onboarding dejó
    # de inventar: un `None` (el dueño no contestó) y un `Decimal("0")` (contestó
    # que no gasta nada) son datos distintos y no pueden colapsar en el mismo
    # número. Con `is not None` el estimado del perfil solo entra si existe; si
    # no, el fallback a cero es una decisión propia de esta capa, no un dato del
    # negocio disfrazado.
    monthly_inventory_cost_est = _estimado_o_cero(
        real_inventory_cost, profile.monthly_inventory_spend_estimate_ars
    )
    monthly_fixed_expenses_est = _estimado_o_cero(
        real_fixed_expenses, profile.monthly_fixed_expenses_estimate_ars
    )
    # Tiering de la fuente de caja (de más a menos confiable):
    #   1. arqueo       → saldo medido (CashClose.counted_total_ars)
    #   2. onboarding   → estimado del perfil (solo si onboarding_recent)
    #   3. flujo        → hay movimientos líquidos pero no saldo → modo cobertura
    #   4. desconocido  → sin nada → caja excluida del score (no-invention)
    has_liquid_movements = liquid_inflow_est > 0 or liquid_outflow_est > 0
    if latest_counted_cash is not None:
        cash_on_hand_est = Decimal(str(latest_counted_cash))
        cash_source = "arqueo"
    elif onboarding_recent and profile.cash_on_hand_estimate_ars is not None:
        cash_on_hand_est = profile.cash_on_hand_estimate_ars
        cash_source = "onboarding"
    elif has_liquid_movements:
        cash_on_hand_est = Decimal("0")  # sin saldo; el score usa cobertura de flujo
        cash_source = "flujo"
    else:
        cash_on_hand_est = Decimal("0")
        cash_source = "desconocido"

    product_count = (
        real_product_count if real_product_count > 0 else (profile.product_count_estimate or 0)
    )
    supplier_count = (
        real_supplier_count if real_supplier_count > 0 else (profile.supplier_count_estimate or 0)
    )

    # ── 7. Completeness scoring ──────────────────────────────────────────────
    has_sales = monthly_sales_est > 0
    has_inventory_cost = monthly_inventory_cost_est > 0
    has_fixed_expenses = monthly_fixed_expenses_est > 0
    # Caja "presente" si hay cualquier fuente real (arqueo, estimado o flujo líquido).
    has_cash_on_hand = cash_source != "desconocido"

    # Procedencia de cada input: ¿lo respalda un dato del negocio, o sólo la
    # respuesta del dueño en el alta? Es la distinción que `_compute_completeness`
    # necesita para no confundir "contestó la encuesta" con "tengo datos".
    # La caja es el único caso con tres fuentes: "arqueo" es un saldo medido y
    # "flujo" sale de movimientos reales; "onboarding" es puramente declarado.
    completeness = _compute_completeness(
        has_sales=has_sales,
        has_inventory_cost=has_inventory_cost,
        has_fixed_expenses=has_fixed_expenses,
        has_cash_on_hand=has_cash_on_hand,
        product_count=product_count,
        supplier_count=supplier_count,
        # Ventas es el ÚNICO input mezclado: con `sale_count < 10`,
        # `monthly_sales_est` sigue siendo 70% la estimación declarada (ver el
        # blend más arriba). Contar una sola venta real como "observado" le
        # daría los 25 puntos completos a un revenue mayormente declarado —
        # justo lo que este cambio existe para evitar. Se usa el mismo corte
        # que el blend: recién en 10 el dato real pasa a dominar.
        sales_observado=sale_count >= 10,
        inventory_cost_observado=real_inventory_cost > 0,
        fixed_expenses_observado=real_fixed_expenses > 0,
        # A propósito NO se mira `cash_source`: su tiering prefiere el estimado
        # del onboarding por sobre el flujo durante los 7 días del alta, así que
        # atarse a él haría que la caja de un tenant con movimientos reales
        # valiera 8 el día 1 y 20 el día 8 sin que cambie un solo dato. Acá la
        # pregunta es otra: ¿hay evidencia real de caja, sin importar qué fuente
        # ganó el desempate?
        cash_observado=latest_counted_cash is not None or has_liquid_movements,
        products_observado=real_product_count > 0,
        suppliers_observado=real_supplier_count > 0,
    )
    confidence = _derive_confidence(completeness)
    main_concern = _derive_main_concern(
        score=completeness,
        has_sales=has_sales,
        has_inventory_cost=has_inventory_cost,
        has_fixed_expenses=has_fixed_expenses,
    )

    # ── 8. Persist BusinessSnapshot (immutable) ──────────────────────────────
    snapshot = BusinessSnapshot(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        snapshot_date=now,
        snapshot_version="v1",
        raw_inputs_json={
            "sale_count": sale_count,
            "expense_count": expense_count,
            "product_count": real_product_count,
            "supplier_count": real_supplier_count,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
        },
        data_completeness_score=Decimal(str(round(completeness, 2))),
        data_mode=profile.data_mode,
        confidence_level=confidence,
        created_at=now,
    )
    session.add(snapshot)
    await session.flush()
    await session.commit()

    # ── 9. Build result ──────────────────────────────────────────────────────
    product_summaries = [
        ProductSummary(
            product_id=p.id,
            name=p.name,
            stock_units=p.stock_units,
            low_stock_threshold_units=p.low_stock_threshold_units,
            sale_price_ars=p.sale_price_ars,
            units_sold_30d=sold_units_by_product.get(p.id, 0),
        )
        for p in active_products
    ]

    state = BusinessState(
        snapshot_id=snapshot.id,
        tenant_id=tenant_id,
        vertical_code=profile.vertical_code,
        data_completeness_score=completeness,
        confidence_level=confidence,
        monthly_sales_est=monthly_sales_est,
        monthly_inventory_cost_est=monthly_inventory_cost_est,
        monthly_fixed_expenses_est=monthly_fixed_expenses_est,
        cash_on_hand_est=cash_on_hand_est,
        product_count=product_count,
        supplier_count=supplier_count,
        products=product_summaries,
        main_concern=main_concern,
        prev_monthly_sales_est=prev_real_sales,
        cash_source=cash_source,
        liquid_inflow_est=liquid_inflow_est,
        liquid_outflow_est=liquid_outflow_est,
    )

    # ── 10. Update Redis cache ───────────────────────────────────────────────
    serialized = _serialize_state(state)
    await redis.set(_cache_key(tenant_id), serialized, ex=CACHE_TTL_SECONDS)
    await redis.set(_hash_key(tenant_id), fingerprint, ex=CACHE_TTL_SECONDS)
    logger.info(
        "bsl.computed",
        tenant_id=str(tenant_id),
        completeness=completeness,
        confidence=confidence,
        snapshot_id=str(snapshot.id),
    )

    return state
