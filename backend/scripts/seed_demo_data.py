#!/usr/bin/env python3
"""
Seed demo data for Véktor.

Creates a kiosco tenant (demo@vektor.app) pre-loaded with realistic data:
  - Score: 74, delta: +8 vs previous week
  - Primary risk: SUPPLIER_DEPENDENCY (low severity for demo)
  - Momentum: 3 weeks of improvement, M1 + M2 unlocked
  - Valor protegido: 85.000 ARS
  - data_completeness_score: 85 (HIGH confidence)

Usage:
    make seed-demo     # Create demo tenant + data
    make reset-demo    # Drop demo tenant and re-seed
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

# Allow running from repo root or backend/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from passlib.context import CryptContext
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)

# ── Constants ─────────────────────────────────────────────────────────────────

DEMO_EMAIL = os.environ.get("DEMO_EMAIL", "demo@vektor.app")
DEMO_PASSWORD = os.environ.get("DEMO_PASSWORD", "demo1234!")
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://vektor:vektor@localhost:5432/vektor",
)

NOW = datetime.now(timezone.utc)
TODAY = date.today()


def _weeks_ago(n: int) -> date:
    return TODAY - timedelta(weeks=n)


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _find_demo_tenant(session: AsyncSession) -> uuid.UUID | None:
    from app.persistence.models.user import User  # noqa: PLC0415

    row = await session.scalar(select(User.tenant_id).where(User.email == DEMO_EMAIL))
    return row


async def _drop_demo_tenant(session: AsyncSession) -> None:
    """Delete the demo tenant and all cascaded data."""
    from app.persistence.models.tenant import Tenant  # noqa: PLC0415
    from app.persistence.models.user import User  # noqa: PLC0415

    tenant_id = await _find_demo_tenant(session)
    if tenant_id is None:
        print("  Demo tenant not found — nothing to drop.")
        return
    await session.execute(delete(Tenant).where(Tenant.tenant_id == tenant_id))
    await session.commit()
    print(f"  Dropped demo tenant {tenant_id}.")


async def _seed(session: AsyncSession) -> None:
    from app.persistence.models.business import (  # noqa: PLC0415
        ActionSuggestion,
        BusinessProfile,
        BusinessSnapshot,
        Insight,
        MomentumProfile,
    )
    from app.persistence.models.score import (  # noqa: PLC0415
        HealthScoreSnapshot,
        WeeklyScoreHistory,
    )
    from app.persistence.models.tenant import Subscription, Tenant  # noqa: PLC0415
    from app.persistence.models.transaction import ExpenseEntry, SaleEntry  # noqa: PLC0415
    from app.persistence.models.user import User  # noqa: PLC0415

    # ── Tenant ────────────────────────────────────────────────────────────────
    tenant_id = uuid.uuid4()
    tenant = Tenant(
        tenant_id=tenant_id,
        legal_name="Demo Kiosco SRL",
        display_name="Kiosco Demo",
        currency="ARS",
        pricing_reference_mode="MEP",
        status="ACTIVE",
    )
    session.add(tenant)

    # ── Subscription ──────────────────────────────────────────────────────────
    session.add(
        Subscription(
            subscription_id=uuid.uuid4(),
            tenant_id=tenant_id,
            plan_code="FREE",
            seats_included=1,
            status="ACTIVE",
            current_period_start=TODAY,
            current_period_end=TODAY + timedelta(days=30),
        )
    )

    # ── User ──────────────────────────────────────────────────────────────────
    user_id = uuid.uuid4()
    session.add(
        User(
            user_id=user_id,
            tenant_id=tenant_id,
            email=DEMO_EMAIL,
            password_hash=_pwd.hash(DEMO_PASSWORD),
            full_name="Usuario Demo",
            role_code="OWNER",
            is_active=True,
        )
    )

    # ── Business Profile ──────────────────────────────────────────────────────
    session.add(
        BusinessProfile(
            profile_id=uuid.uuid4(),
            tenant_id=tenant_id,
            vertical_code="kiosco",
            data_mode="M2",
            data_confidence="HIGH",
            monthly_sales_estimate_ars=Decimal("280000"),
            monthly_inventory_spend_estimate_ars=Decimal("140000"),
            monthly_fixed_expenses_estimate_ars=Decimal("45000"),
            cash_on_hand_estimate_ars=Decimal("32000"),
            supplier_count_estimate=3,
            product_count_estimate=120,
            onboarding_completed=True,
            heuristic_profile_version="v1",
        )
    )

    # ── Business Snapshot (current) ───────────────────────────────────────────
    snap_id = uuid.uuid4()
    session.add(
        BusinessSnapshot(
            id=snap_id,
            tenant_id=tenant_id,
            snapshot_date=NOW,
            snapshot_version="v1",
            data_completeness_score=Decimal("85.00"),
            data_mode="M2",
            confidence_level="HIGH",
            raw_inputs_json={
                "monthly_sales": 280000,
                "monthly_expenses": 185000,
                "cash_on_hand": 32000,
                "supplier_count": 3,
            },
            created_at=NOW,
        )
    )

    # ── Health Score Snapshots (4 weeks) ──────────────────────────────────────
    weekly_scores = [
        (4, 62, "MEDIUM"),
        (3, 66, "MEDIUM"),
        (2, 70, "GOOD"),
        (1, 74, "GOOD"),
    ]
    latest_score_id = uuid.uuid4()
    for i, (weeks_back, score, level) in enumerate(weekly_scores):
        snap_date = datetime.combine(
            _weeks_ago(weeks_back), datetime.min.time(), tzinfo=timezone.utc
        )
        sid = latest_score_id if weeks_back == 1 else uuid.uuid4()
        session.add(
            HealthScoreSnapshot(
                id=sid,
                tenant_id=tenant_id,
                total_score=Decimal(str(score)),
                level=level,
                dimensions={
                    "cash": {"score": score - 2, "label": level},
                    "margin": {"score": score + 2, "label": level},
                    "stock": {"score": score - 5, "label": "MEDIUM"},
                    "supplier": {"score": score - 10, "label": "MEDIUM"},
                },
                triggered_by="seed_demo",
                snapshot_date=snap_date,
                created_at=snap_date,
                score_cash=score - 2,
                score_margin=score + 2,
                score_stock=score - 5,
                score_supplier=score - 10,
                source_snapshot_id=snap_id if weeks_back == 1 else None,
                heuristic_version="v1",
                primary_risk_code="SUPPLIER_DEPENDENCY",
                confidence_level="HIGH",
                data_completeness_score=Decimal("85.00"),
            )
        )

    # ── Weekly Score History ───────────────────────────────────────────────────
    history = [
        (4, 62, 60, 64, "MEDIUM", None, None),
        (3, 66, 64, 68, "MEDIUM", Decimal("4.00"), "IMPROVING"),
        (2, 70, 68, 72, "GOOD", Decimal("4.00"), "IMPROVING"),
        (1, 74, 72, 76, "GOOD", Decimal("8.00"), "IMPROVING"),
    ]
    for weeks_back, avg, mn, mx, level, delta, trend in history:
        w_start = _weeks_ago(weeks_back)
        session.add(
            WeeklyScoreHistory(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                week_start=w_start,
                week_end=w_start + timedelta(days=6),
                avg_score=Decimal(str(avg)),
                min_score=Decimal(str(mn)),
                max_score=Decimal(str(mx)),
                level=level,
                created_at=datetime.combine(w_start, datetime.min.time(), tzinfo=timezone.utc),
                delta=delta,
                trend_label=trend,
            )
        )

    # ── Momentum Profile ──────────────────────────────────────────────────────
    session.add(
        MomentumProfile(
            tenant_id=tenant_id,
            best_score_ever=74,
            best_score_date=TODAY,
            improving_streak_weeks=3,
            estimated_value_protected_ars=Decimal("85000.00"),
            milestones_json=[
                {"id": "M1", "label": "Primera semana de mejora", "unlocked": True,
                 "unlocked_at": (_weeks_ago(2)).isoformat()},
                {"id": "M2", "label": "Tres semanas seguidas mejorando", "unlocked": True,
                 "unlocked_at": TODAY.isoformat()},
            ],
            active_goal_json={"target_score": 80, "deadline_weeks": 4},
            updated_at=NOW,
        )
    )

    # ── Insight ───────────────────────────────────────────────────────────────
    insight_id = uuid.uuid4()
    session.add(
        Insight(
            id=insight_id,
            tenant_id=tenant_id,
            insight_type="SUPPLIER_DEPENDENCY",
            title="Concentración de proveedores",
            description=(
                "El 70% de tu inventario viene de un solo proveedor. "
                "Si falla el suministro podés quedarte sin stock en 5 días."
            ),
            severity_code="LOW",
            heuristic_version="v1",
        )
    )

    # ── Action Suggestion ─────────────────────────────────────────────────────
    session.add(
        ActionSuggestion(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            insight_id=insight_id,
            action_type="DIVERSIFY_SUPPLIERS",
            title="Sumá un segundo proveedor para tu línea de snacks",
            description=(
                "Contactá al menos un proveedor alternativo esta semana. "
                "Con dos fuentes, reducís el riesgo de quiebre de stock en un 60%."
            ),
            risk_level="LOW",
            status="SUGGESTED",
        )
    )

    # ── Sales (last 30 days sample) ────────────────────────────────────────────
    for i in range(30):
        sale_date = TODAY - timedelta(days=i)
        # Mon-Fri slightly higher than weekends
        amount = Decimal("9200") if sale_date.weekday() < 5 else Decimal("6500")
        session.add(
            SaleEntry(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                amount=amount,
                quantity=1,
                transaction_date=sale_date,
                payment_method="cash",
                notes="demo",
            )
        )

    # ── Expenses (last 30 days sample) ────────────────────────────────────────
    expense_data = [
        ("rent", Decimal("35000"), True, "Alquiler mensual", "transfer"),
        ("utilities", Decimal("4500"), True, "Luz y gas", "transfer"),
        ("supplies", Decimal("140000"), False, "Reposición de mercadería", "transfer"),
        ("cleaning", Decimal("2000"), False, "Productos de limpieza", "cash"),
        ("misc", Decimal("1800"), False, "Varios", "cash"),
    ]
    for i, (cat, amount, recurring, desc, method) in enumerate(expense_data):
        session.add(
            ExpenseEntry(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                amount=amount,
                category=cat,
                transaction_date=TODAY - timedelta(days=i * 6),
                description=desc,
                is_recurring=recurring,
                payment_method=method,
            )
        )

    await session.commit()
    print(f"  Demo tenant created: {tenant_id}")
    print(f"  Login: {DEMO_EMAIL} / {DEMO_PASSWORD}")
    print("  Score: 74 (+8 vs prev week) · Risk: SUPPLIER_DEPENDENCY · Momentum: 3w streak")


# ── Entry points ──────────────────────────────────────────────────────────────


async def main(reset: bool = False) -> None:
    engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as session:
        if reset:
            print("Resetting demo data...")
            await _drop_demo_tenant(session)

        existing = await _find_demo_tenant(session)
        if existing and not reset:
            print(f"Demo tenant already exists ({existing}). Use 'make reset-demo' to re-seed.")
            await engine.dispose()
            return

        print("Seeding demo data...")
        await _seed(session)

    await engine.dispose()
    print("Done.")


if __name__ == "__main__":
    reset_flag = "--reset" in sys.argv
    asyncio.run(main(reset=reset_flag))
