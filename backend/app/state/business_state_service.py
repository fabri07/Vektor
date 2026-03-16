"""
Business State Service.

Computes a rich, denormalized BusinessState for a tenant by combining:
  - Real transaction data (last 30 days) — preferred source.
  - Onboarding estimates from BusinessProfile — fallback when real data is absent.

This state is the ONLY input to the Health Engine; no raw DB data ever
reaches score computation directly.

Cache strategy
--------------
  Redis key  : business_state:{tenant_id}        — JSON blob, TTL 24 h
  Redis key  : last_inputs_hash:{tenant_id}      — SHA-256 of input fingerprint, TTL 24 h

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

from app.heuristics.base import VerticalRules
from app.heuristics.decoracion import DecoracionHogarHeuristicRuleSet
from app.heuristics.kiosco import KioscoHeuristicRuleSet
from app.heuristics.limpieza import LimpiezaHeuristicRuleSet
from app.observability.logger import get_logger
from app.persistence.models.business import BusinessProfile, BusinessSnapshot
from app.persistence.models.product import Product
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.persistence.repositories.business_profile_repository import BusinessProfileRepository

logger = get_logger(__name__)

# ── Heuristic registry ─────────────────────────────────────────────────────────

_RULESET_INSTANCES: dict[str, KioscoHeuristicRuleSet | DecoracionHogarHeuristicRuleSet | LimpiezaHeuristicRuleSet] = {
    "kiosco": KioscoHeuristicRuleSet(),
    "decoracion_hogar": DecoracionHogarHeuristicRuleSet(),
    "limpieza": LimpiezaHeuristicRuleSet(),
}

CACHE_TTL_SECONDS = 60 * 60 * 24  # 24 h


# ── Dataclasses ────────────────────────────────────────────────────────────────


@dataclass
class ProductSummary:
    product_id: UUID
    name: str
    stock_units: int
    low_stock_threshold_units: int
    sale_price_ars: Decimal


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
    confidence_level: str          # HIGH | MEDIUM | LOW
    ruleset: VerticalRules
    monthly_sales_est: Decimal
    monthly_inventory_cost_est: Decimal
    monthly_fixed_expenses_est: Decimal
    cash_on_hand_est: Decimal
    product_count: int
    supplier_count: int
    products: list[ProductSummary]
    main_concern: str | None


# ── Cache helpers ──────────────────────────────────────────────────────────────


def _cache_key(tenant_id: UUID) -> str:
    return f"business_state:{tenant_id}"


def _hash_key(tenant_id: UUID) -> str:
    return f"last_inputs_hash:{tenant_id}"


def _make_fingerprint(
    sale_count: int,
    expense_count: int,
    product_count: int,
    profile_updated_at: datetime,
) -> str:
    raw = f"{sale_count}:{expense_count}:{product_count}:{profile_updated_at.isoformat()}"
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
    # ruleset is not JSON-serializable; store vertical_code only (re-loaded on deserialize)
    d["ruleset"] = state.ruleset.vertical
    d["products"] = [
        {
            "product_id": str(p.product_id),
            "name": p.name,
            "stock_units": p.stock_units,
            "low_stock_threshold_units": p.low_stock_threshold_units,
            "sale_price_ars": str(p.sale_price_ars),
        }
        for p in state.products
    ]
    return json.dumps(d)


def _deserialize_state(raw: str) -> BusinessState:
    """Restore BusinessState from JSON string."""
    d = json.loads(raw)
    vertical_code: str = d["ruleset"]
    ruleset_instance = _RULESET_INSTANCES.get(vertical_code)
    if ruleset_instance is None:
        raise ValueError(f"Unknown vertical in cached state: {vertical_code!r}")
    products = [
        ProductSummary(
            product_id=UUID(p["product_id"]),
            name=p["name"],
            stock_units=p["stock_units"],
            low_stock_threshold_units=p.get("low_stock_threshold_units", 0),
            sale_price_ars=Decimal(p["sale_price_ars"]),
        )
        for p in d["products"]
    ]
    return BusinessState(
        snapshot_id=UUID(d["snapshot_id"]),
        tenant_id=UUID(d["tenant_id"]),
        vertical_code=d["vertical_code"],
        data_completeness_score=float(d["data_completeness_score"]),
        confidence_level=d["confidence_level"],
        ruleset=ruleset_instance.get_rules(),
        monthly_sales_est=Decimal(d["monthly_sales_est"]),
        monthly_inventory_cost_est=Decimal(d["monthly_inventory_cost_est"]),
        monthly_fixed_expenses_est=Decimal(d["monthly_fixed_expenses_est"]),
        cash_on_hand_est=Decimal(d["cash_on_hand_est"]),
        product_count=d["product_count"],
        supplier_count=d["supplier_count"],
        products=products,
        main_concern=d.get("main_concern"),
    )


# ── Completeness scoring ───────────────────────────────────────────────────────


def _compute_completeness(
    has_sales: bool,
    has_inventory_cost: bool,
    has_fixed_expenses: bool,
    has_cash_on_hand: bool,
    product_count: int,
    supplier_count: int,
) -> float:
    score = 0.0
    if has_sales:
        score += 25.0
    if has_inventory_cost:
        score += 20.0
    if has_fixed_expenses:
        score += 15.0
    if has_cash_on_hand:
        score += 20.0
    if product_count >= 5:
        score += 10.0
    if supplier_count >= 1:
        score += 10.0
    return score


def _derive_confidence(score: float) -> str:
    if score >= 80.0:
        return "HIGH"
    if score >= 50.0:
        return "MEDIUM"
    return "LOW"


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

    ruleset_instance = _RULESET_INSTANCES.get(profile.vertical_code)
    if ruleset_instance is None:
        raise ValueError(f"Unknown vertical_code: {profile.vertical_code!r}")

    # ── 2. Aggregate sales (last 30 days) ────────────────────────────────────
    sale_sum_result = await session.execute(
        select(func.sum(SaleEntry.amount), func.count(SaleEntry.id)).where(
            SaleEntry.tenant_id == tenant_id,
            SaleEntry.transaction_date >= window_start,
            SaleEntry.transaction_date <= window_end,
        )
    )
    sale_sum_row = sale_sum_result.one()
    real_sales: Decimal = Decimal(str(sale_sum_row[0] or 0))
    sale_count: int = int(sale_sum_row[1] or 0)

    # ── 3. Aggregate expenses (last 30 days) ─────────────────────────────────
    # mercaderia cost = expenses categorized as 'mercaderia'
    inv_sum_result = await session.execute(
        select(func.sum(ExpenseEntry.amount), func.count(ExpenseEntry.id)).where(
            ExpenseEntry.tenant_id == tenant_id,
            ExpenseEntry.transaction_date >= window_start,
            ExpenseEntry.transaction_date <= window_end,
            ExpenseEntry.category == "mercaderia",
        )
    )
    inv_row = inv_sum_result.one()
    real_inventory_cost: Decimal = Decimal(str(inv_row[0] or 0))

    # fixed expenses = is_recurring = True
    fixed_sum_result = await session.execute(
        select(func.sum(ExpenseEntry.amount), func.count(ExpenseEntry.id)).where(
            ExpenseEntry.tenant_id == tenant_id,
            ExpenseEntry.transaction_date >= window_start,
            ExpenseEntry.transaction_date <= window_end,
            ExpenseEntry.is_recurring.is_(True),
        )
    )
    fixed_row = fixed_sum_result.one()
    real_fixed_expenses: Decimal = Decimal(str(fixed_row[0] or 0))

    # total expense count (for fingerprint)
    expense_count_result = await session.execute(
        select(func.count(ExpenseEntry.id)).where(
            ExpenseEntry.tenant_id == tenant_id,
            ExpenseEntry.transaction_date >= window_start,
            ExpenseEntry.transaction_date <= window_end,
        )
    )
    expense_count: int = int(expense_count_result.scalar_one() or 0)

    # unique supplier names
    supplier_result = await session.execute(
        select(func.count(ExpenseEntry.supplier_name.distinct())).where(
            ExpenseEntry.tenant_id == tenant_id,
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

    # ── 5. Cache fingerprint check ───────────────────────────────────────────
    fingerprint = _make_fingerprint(
        sale_count=sale_count,
        expense_count=expense_count,
        product_count=real_product_count,
        profile_updated_at=profile.updated_at,
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
    onboarding_recent = (
        profile.onboarding_completed
        and _profile_updated_at >= datetime.now(UTC) - timedelta(days=7)
    )

    monthly_sales_est = real_sales if sale_count > 0 else (
        profile.monthly_sales_estimate_ars or Decimal("0")
    )
    monthly_inventory_cost_est = real_inventory_cost if real_inventory_cost > 0 else (
        profile.monthly_inventory_spend_estimate_ars or Decimal("0")
    )
    monthly_fixed_expenses_est = real_fixed_expenses if real_fixed_expenses > 0 else (
        profile.monthly_fixed_expenses_estimate_ars or Decimal("0")
    )
    cash_on_hand_est = (
        profile.cash_on_hand_estimate_ars
        if onboarding_recent and profile.cash_on_hand_estimate_ars is not None
        else Decimal("0")
    )

    product_count = real_product_count if real_product_count > 0 else (
        profile.product_count_estimate or 0
    )
    supplier_count = real_supplier_count if real_supplier_count > 0 else (
        profile.supplier_count_estimate or 0
    )

    # ── 7. Completeness scoring ──────────────────────────────────────────────
    has_sales = monthly_sales_est > 0
    has_inventory_cost = monthly_inventory_cost_est > 0
    has_fixed_expenses = monthly_fixed_expenses_est > 0
    has_cash_on_hand = cash_on_hand_est > 0

    completeness = _compute_completeness(
        has_sales=has_sales,
        has_inventory_cost=has_inventory_cost,
        has_fixed_expenses=has_fixed_expenses,
        has_cash_on_hand=has_cash_on_hand,
        product_count=product_count,
        supplier_count=supplier_count,
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
        )
        for p in active_products
    ]

    state = BusinessState(
        snapshot_id=snapshot.id,
        tenant_id=tenant_id,
        vertical_code=profile.vertical_code,
        data_completeness_score=completeness,
        confidence_level=confidence,
        ruleset=ruleset_instance.get_rules(),
        monthly_sales_est=monthly_sales_est,
        monthly_inventory_cost_est=monthly_inventory_cost_est,
        monthly_fixed_expenses_est=monthly_fixed_expenses_est,
        cash_on_hand_est=cash_on_hand_est,
        product_count=product_count,
        supplier_count=supplier_count,
        products=product_summaries,
        main_concern=main_concern,
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
