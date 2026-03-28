#!/usr/bin/env python3
"""
Seed demo data for Véktor — F5-03.

Creates 3 demo tenants with calibrated realistic data:

  TENANT 1 — Kiosco San Martín (demo.kiosco@vektor.app)
    Score 74 | Saludable | Streak 3w | M1+M2 desbloqueados

  TENANT 2 — Distribuidora Clean (demo.limpieza@vektor.app)
    Score 51 | En riesgo | Sin streak | Caja crítica

  TENANT 3 — Casa & Deco Palermo (demo.deco@vektor.app)
    Score 62 | Estable | Streak 1w | Stock inmovilizado

Usage:
    make seed-demo     # Crea los 3 tenants (idempotente)
    make reset-demo    # Elimina y re-crea los 3 tenants
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from passlib.context import CryptContext
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)

# ── Constants ─────────────────────────────────────────────────────────────────

DEMO_PASSWORD = os.environ.get("DEMO_PASSWORD", "Demo1234!")
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://vektor:vektor@localhost:5432/vektor",
)

DEMO_TENANTS = [
    "demo.kiosco@vektor.app",
    "demo.limpieza@vektor.app",
    "demo.deco@vektor.app",
]

NOW = datetime.now(timezone.utc)
TODAY = date.today()


def _weeks_ago(n: int) -> date:
    return TODAY - timedelta(weeks=n)


def _score_level(score: int) -> str:
    if score >= 70:
        return "GOOD"
    if score >= 40:
        return "MEDIUM"
    return "CRITICAL"


# ── Drop helpers ──────────────────────────────────────────────────────────────


async def _find_tenant_by_email(session: AsyncSession, email: str) -> uuid.UUID | None:
    from app.persistence.models.user import User  # noqa: PLC0415

    return await session.scalar(select(User.tenant_id).where(User.email == email))


async def _drop_demo_tenants(session: AsyncSession) -> None:
    from app.persistence.models.tenant import Tenant  # noqa: PLC0415

    dropped = 0
    for email in DEMO_TENANTS:
        tenant_id = await _find_tenant_by_email(session, email)
        if tenant_id is not None:
            await session.execute(delete(Tenant).where(Tenant.tenant_id == tenant_id))
            print(f"  Dropped tenant for {email} ({tenant_id})")
            dropped += 1
    if dropped:
        await session.commit()
    else:
        print("  No demo tenants found — nothing to drop.")


# ── Seed helpers ──────────────────────────────────────────────────────────────


async def _seed_kiosco(session: AsyncSession) -> None:
    """Kiosco San Martín — Score 74, healthy, streak 3w, M1+M2."""
    from app.persistence.models.business import (  # noqa: PLC0415
        ActionSuggestion,
        BusinessProfile,
        BusinessSnapshot,
        Insight,
        MomentumProfile,
    )
    from app.persistence.models.notification import Notification  # noqa: PLC0415
    from app.persistence.models.product import Product  # noqa: PLC0415
    from app.persistence.models.score import (  # noqa: PLC0415
        HealthScoreSnapshot,
        WeeklyScoreHistory,
    )
    from app.persistence.models.tenant import Subscription, Tenant  # noqa: PLC0415
    from app.persistence.models.transaction import ExpenseEntry, SaleEntry  # noqa: PLC0415
    from app.persistence.models.user import User  # noqa: PLC0415

    tenant_id = uuid.uuid4()
    email = "demo.kiosco@vektor.app"

    session.add(
        Tenant(
            tenant_id=tenant_id,
            legal_name="Kiosco San Martín SRL",
            display_name="Kiosco San Martín",
            currency="ARS",
            pricing_reference_mode="MEP",
            status="ACTIVE",
        )
    )
    await session.flush()
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
    user_id = uuid.uuid4()
    session.add(
        User(
            user_id=user_id,
            tenant_id=tenant_id,
            email=email,
            password_hash=_pwd.hash(DEMO_PASSWORD),
            full_name="Martín Rodríguez",
            role_code="OWNER",
            is_active=True,
        )
    )
    session.add(
        BusinessProfile(
            profile_id=uuid.uuid4(),
            tenant_id=tenant_id,
            vertical_code="kiosco",
            data_mode="M1",
            data_confidence="HIGH",
            monthly_sales_estimate_ars=Decimal("3200000"),
            monthly_inventory_spend_estimate_ars=Decimal("1900000"),
            monthly_fixed_expenses_estimate_ars=Decimal("380000"),
            cash_on_hand_estimate_ars=Decimal("620000"),
            supplier_count_estimate=3,
            product_count_estimate=180,
            onboarding_completed=True,
            heuristic_profile_version="v1",
        )
    )
    snap_id = uuid.uuid4()
    session.add(
        BusinessSnapshot(
            id=snap_id,
            tenant_id=tenant_id,
            snapshot_date=NOW,
            snapshot_version="v1",
            data_completeness_score=Decimal("85.00"),
            data_mode="M1",
            confidence_level="HIGH",
            raw_inputs_json={
                "monthly_sales": 3200000,
                "monthly_expenses": 2280000,
                "cash_on_hand": 620000,
                "supplier_count": 3,
            },
            created_at=NOW,
        )
    )

    # 8 weeks of history — steady improvement
    weekly_scores = [58, 61, 64, 67, 69, 71, 73, 74]
    latest_score_id = uuid.uuid4()

    for i, score in enumerate(weekly_scores):
        weeks_back = len(weekly_scores) - i  # 8→1
        snap_date = datetime.combine(
            _weeks_ago(weeks_back), datetime.min.time(), tzinfo=timezone.utc
        )
        sid = latest_score_id if weeks_back == 1 else uuid.uuid4()
        session.add(
            HealthScoreSnapshot(
                id=sid,
                tenant_id=tenant_id,
                total_score=Decimal(str(score)),
                level=_score_level(score),
                dimensions={
                    "cash":     {"score": 78 if weeks_back == 1 else score + 4, "label": _score_level(score)},
                    "margin":   {"score": 72 if weeks_back == 1 else score - 2, "label": _score_level(score)},
                    "stock":    {"score": 76 if weeks_back == 1 else score + 2, "label": _score_level(score)},
                    "supplier": {"score": 68 if weeks_back == 1 else score - 6, "label": "MEDIUM"},
                },
                triggered_by="seed_demo",
                snapshot_date=snap_date,
                created_at=snap_date,
                score_cash=78 if weeks_back == 1 else score + 4,
                score_margin=72 if weeks_back == 1 else score - 2,
                score_stock=76 if weeks_back == 1 else score + 2,
                score_supplier=68 if weeks_back == 1 else score - 6,
                source_snapshot_id=snap_id if weeks_back == 1 else None,
                heuristic_version="v1",
                primary_risk_code="SUPPLIER_DEPENDENCY",
                confidence_level="HIGH",
                data_completeness_score=Decimal("85.00"),
            )
        )

    for i, score in enumerate(weekly_scores):
        weeks_back = len(weekly_scores) - i
        w_start = _weeks_ago(weeks_back)
        prev_score = weekly_scores[i - 1] if i > 0 else None
        delta = Decimal(str(score - prev_score)) if prev_score is not None else None
        session.add(
            WeeklyScoreHistory(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                week_start=w_start,
                week_end=w_start + timedelta(days=6),
                avg_score=Decimal(str(score)),
                min_score=Decimal(str(score - 2)),
                max_score=Decimal(str(score + 2)),
                level=_score_level(score),
                created_at=datetime.combine(w_start, datetime.min.time(), tzinfo=timezone.utc),
                delta=delta,
                trend_label="IMPROVING" if delta and delta > 0 else None,
            )
        )

    session.add(
        MomentumProfile(
            tenant_id=tenant_id,
            best_score_ever=74,
            best_score_date=TODAY,
            improving_streak_weeks=3,
            estimated_value_protected_ars=Decimal("185000.00"),
            milestones_json=[
                {
                    "id": "M1",
                    "label": "Primera semana de mejora",
                    "unlocked": True,
                    "unlocked_at": _weeks_ago(2).isoformat(),
                },
                {
                    "id": "M2",
                    "label": "Tres semanas seguidas mejorando",
                    "unlocked": True,
                    "unlocked_at": TODAY.isoformat(),
                },
            ],
            active_goal_json={"target_score": 80, "deadline_weeks": 4},
            updated_at=NOW,
        )
    )

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
    await session.flush()
    session.add(
        ActionSuggestion(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            insight_id=insight_id,
            action_type="DIVERSIFY_SUPPLIERS",
            title="Sumá un segundo proveedor para tu línea de snacks",
            description=(
                "Contactá al menos un proveedor alternativo esta semana. "
                "Con dos fuentes reducís el riesgo de quiebre de stock en un 60%."
            ),
            risk_level="LOW",
            status="SUGGESTED",
        )
    )

    # 15 productos kiosco
    kiosco_products = [
        ("Coca-Cola 2.25L",           "BEB-001", "bebidas",    Decimal("1400"), Decimal("850"),  48, 12),
        ("Cigarrillos Marlboro x10",  "TAB-001", "tabaco",     Decimal("2200"), Decimal("1800"), 24,  6),
        ("Alfajor Havanna",           "DUL-001", "dulces",     Decimal("700"),  Decimal("420"),  36, 10),
        ("Agua mineral 500ml",        "BEB-002", "bebidas",    Decimal("500"),  Decimal("280"),  60, 20),
        ("Lavandina 1L",              "LIM-001", "limpieza",   Decimal("550"),  Decimal("320"),  24,  6),
        ("Sprite 2.25L",              "BEB-003", "bebidas",    Decimal("1350"), Decimal("820"),  36, 12),
        ("Palitos Traviesos",         "SNA-001", "snacks",     Decimal("350"),  Decimal("180"),  48, 12),
        ("Chicles Topline x12",       "DUL-002", "dulces",     Decimal("280"),  Decimal("150"),  72, 20),
        ("Café Nescafé 200g",         "INF-001", "infusiones", Decimal("2000"), Decimal("1200"), 12,  4),
        ("Galletitas Oreo x3",        "DUL-003", "dulces",     Decimal("1100"), Decimal("650"),  36, 10),
        ("Jugo Tang sobre",           "BEB-004", "bebidas",    Decimal("180"),  Decimal("90"),   96, 30),
        ("Yogur Danone 190g",         "LAC-001", "lacteos",    Decimal("680"),  Decimal("380"),  24,  8),
        ("Flan Casero x4",            "LAC-002", "lacteos",    Decimal("520"),  Decimal("290"),  18,  6),
        ("Vino Tetra Brick 1L",       "BEB-005", "bebidas",    Decimal("1200"), Decimal("750"),  30,  8),
        ("Maní con sal 200g",         "SNA-002", "snacks",     Decimal("420"),  Decimal("220"),  42, 12),
    ]
    for name, sku, cat, price, cost, stock, threshold in kiosco_products:
        session.add(
            Product(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                name=name,
                sku=sku,
                category=cat,
                sale_price_ars=price,
                unit_cost_ars=cost,
                stock_units=stock,
                low_stock_threshold_units=threshold,
                is_active=True,
            )
        )

    # 30 días de ventas
    for i in range(30):
        sale_date = TODAY - timedelta(days=i)
        amount = Decimal("10700") if sale_date.weekday() < 5 else Decimal("7500")
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

    # Gastos del mes
    for cat, amount, recurring, desc, method, days_ago in [
        ("rent",     Decimal("380000"), True,  "Alquiler mensual",          "transfer", 1),
        ("utilities",Decimal("22000"),  True,  "Luz, gas y agua",           "transfer", 5),
        ("supplies", Decimal("1900000"),False, "Reposición de mercadería",  "transfer", 3),
        ("cleaning", Decimal("8000"),   False, "Productos de limpieza",     "cash",     8),
        ("misc",     Decimal("12000"),  False, "Varios y bolsas",           "cash",     12),
    ]:
        session.add(
            ExpenseEntry(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                amount=amount,
                category=cat,
                transaction_date=TODAY - timedelta(days=days_ago),
                description=desc,
                is_recurring=recurring,
                payment_method=method,
            )
        )

    # 3 notificaciones no leídas
    for title, body, ntype in [
        (
            "Tu score mejoró a 74",
            "Llevas 3 semanas de mejora consecutiva. ¡Vas bien!",
            "SCORE_UP",
        ),
        (
            "Riesgo: dependencia de proveedor",
            "El 70% de tu inventario depende de un solo proveedor. Diversificá para estar más protegido.",
            "INSIGHT",
        ),
        (
            "Objetivo: llegar a 80 en 4 semanas",
            "Reducir la concentración de proveedores puede sumar hasta 6 puntos a tu score.",
            "GOAL",
        ),
    ]:
        session.add(
            Notification(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                user_id=user_id,
                title=title,
                body=body,
                notification_type=ntype,
                channel="in_app",
                is_read=False,
            )
        )

    await session.commit()
    print(f"  [kiosco]   {email} → score 74 | streak 3w | M1+M2 | 15 productos")


async def _seed_limpieza(session: AsyncSession) -> None:
    """Distribuidora Clean — Score 51, en riesgo, caja crítica, 1 proveedor."""
    from app.persistence.models.business import (  # noqa: PLC0415
        ActionSuggestion,
        BusinessProfile,
        BusinessSnapshot,
        Insight,
        MomentumProfile,
    )
    from app.persistence.models.notification import Notification  # noqa: PLC0415
    from app.persistence.models.product import Product  # noqa: PLC0415
    from app.persistence.models.score import (  # noqa: PLC0415
        HealthScoreSnapshot,
        WeeklyScoreHistory,
    )
    from app.persistence.models.tenant import Subscription, Tenant  # noqa: PLC0415
    from app.persistence.models.transaction import ExpenseEntry, SaleEntry  # noqa: PLC0415
    from app.persistence.models.user import User  # noqa: PLC0415

    tenant_id = uuid.uuid4()
    email = "demo.limpieza@vektor.app"

    session.add(
        Tenant(
            tenant_id=tenant_id,
            legal_name="Distribuidora Clean SA",
            display_name="Distribuidora Clean",
            currency="ARS",
            pricing_reference_mode="MEP",
            status="ACTIVE",
        )
    )
    await session.flush()
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
    user_id = uuid.uuid4()
    session.add(
        User(
            user_id=user_id,
            tenant_id=tenant_id,
            email=email,
            password_hash=_pwd.hash(DEMO_PASSWORD),
            full_name="Claudia Fernández",
            role_code="OWNER",
            is_active=True,
        )
    )
    session.add(
        BusinessProfile(
            profile_id=uuid.uuid4(),
            tenant_id=tenant_id,
            vertical_code="limpieza",
            data_mode="M1",
            data_confidence="MEDIUM",
            monthly_sales_estimate_ars=Decimal("2800000"),
            monthly_inventory_spend_estimate_ars=Decimal("2100000"),
            monthly_fixed_expenses_estimate_ars=Decimal("320000"),
            cash_on_hand_estimate_ars=Decimal("180000"),
            supplier_count_estimate=1,
            product_count_estimate=85,
            onboarding_completed=True,
            heuristic_profile_version="v1",
        )
    )
    snap_id = uuid.uuid4()
    session.add(
        BusinessSnapshot(
            id=snap_id,
            tenant_id=tenant_id,
            snapshot_date=NOW,
            snapshot_version="v1",
            data_completeness_score=Decimal("70.00"),
            data_mode="M1",
            confidence_level="MEDIUM",
            raw_inputs_json={
                "monthly_sales": 2800000,
                "monthly_expenses": 2420000,
                "cash_on_hand": 180000,
                "supplier_count": 1,
            },
            created_at=NOW,
        )
    )

    # 8 semanas declining: 63→61→59→57→55→53→52→51
    weekly_scores = [63, 61, 59, 57, 55, 53, 52, 51]
    latest_score_id = uuid.uuid4()

    for i, score in enumerate(weekly_scores):
        weeks_back = len(weekly_scores) - i
        snap_date = datetime.combine(
            _weeks_ago(weeks_back), datetime.min.time(), tzinfo=timezone.utc
        )
        sid = latest_score_id if weeks_back == 1 else uuid.uuid4()
        session.add(
            HealthScoreSnapshot(
                id=sid,
                tenant_id=tenant_id,
                total_score=Decimal(str(score)),
                level=_score_level(score),
                dimensions={
                    "cash":     {"score": 42 if weeks_back == 1 else max(30, score - 9), "label": "CRITICAL" if weeks_back == 1 else _score_level(score)},
                    "margin":   {"score": 55 if weeks_back == 1 else score + 4, "label": _score_level(score)},
                    "stock":    {"score": 62 if weeks_back == 1 else score + 11, "label": _score_level(score)},
                    "supplier": {"score": 22 if weeks_back == 1 else max(18, score - 31), "label": "CRITICAL"},
                },
                triggered_by="seed_demo",
                snapshot_date=snap_date,
                created_at=snap_date,
                score_cash=42 if weeks_back == 1 else max(30, score - 9),
                score_margin=55 if weeks_back == 1 else score + 4,
                score_stock=62 if weeks_back == 1 else score + 11,
                score_supplier=22 if weeks_back == 1 else max(18, score - 31),
                source_snapshot_id=snap_id if weeks_back == 1 else None,
                heuristic_version="v1",
                primary_risk_code="CASH_LOW",
                confidence_level="MEDIUM",
                data_completeness_score=Decimal("70.00"),
            )
        )

    for i, score in enumerate(weekly_scores):
        weeks_back = len(weekly_scores) - i
        w_start = _weeks_ago(weeks_back)
        prev_score = weekly_scores[i - 1] if i > 0 else None
        delta = Decimal(str(score - prev_score)) if prev_score is not None else None
        session.add(
            WeeklyScoreHistory(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                week_start=w_start,
                week_end=w_start + timedelta(days=6),
                avg_score=Decimal(str(score)),
                min_score=Decimal(str(score - 1)),
                max_score=Decimal(str(score + 1)),
                level=_score_level(score),
                created_at=datetime.combine(w_start, datetime.min.time(), tzinfo=timezone.utc),
                delta=delta,
                trend_label="DECLINING" if delta and delta < 0 else None,
            )
        )

    session.add(
        MomentumProfile(
            tenant_id=tenant_id,
            best_score_ever=63,
            best_score_date=_weeks_ago(8),
            improving_streak_weeks=0,
            estimated_value_protected_ars=Decimal("42000.00"),
            milestones_json=[],
            active_goal_json={"target_score": 60, "deadline_weeks": 6},
            updated_at=NOW,
        )
    )

    cash_insight_id = uuid.uuid4()
    session.add(
        Insight(
            id=cash_insight_id,
            tenant_id=tenant_id,
            insight_type="CASH_LOW",
            title="Caja crítica: menos de 17 días de operación",
            description=(
                "Con $180.000 en caja y gastos fijos de $320.000/mes, "
                "tu negocio tiene cobertura para 17 días. El mínimo recomendado es 30 días."
            ),
            severity_code="HIGH",
            heuristic_version="v1",
        )
    )
    await session.flush()
    session.add(
        ActionSuggestion(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            insight_id=cash_insight_id,
            action_type="IMPROVE_CASH_FLOW",
            title="Negociá una línea de crédito proveedora esta semana",
            description=(
                "Pedile a tu proveedor principal 30 días de plazo de pago. "
                "Esto libera $320.000 de caja y te da margen para operar sin estrés."
            ),
            risk_level="LOW",
            status="SUGGESTED",
        )
    )

    supplier_insight_id = uuid.uuid4()
    session.add(
        Insight(
            id=supplier_insight_id,
            tenant_id=tenant_id,
            insight_type="SUPPLIER_DEPENDENCY",
            title="Proveedor único: riesgo de quiebre de stock alto",
            description=(
                "Trabajás con un solo proveedor. Si falla el abastecimiento, "
                "tu negocio para en menos de una semana."
            ),
            severity_code="HIGH",
            heuristic_version="v1",
        )
    )
    await session.flush()
    session.add(
        ActionSuggestion(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            insight_id=supplier_insight_id,
            action_type="DIVERSIFY_SUPPLIERS",
            title="Contactá un segundo distribuidor de productos de limpieza",
            description=(
                "Registrá al menos un proveedor alternativo como backup. "
                "Incluso si no lo usás regularmente, tenerlo disponible reduce el riesgo a MEDIUM."
            ),
            risk_level="MEDIUM",
            status="SUGGESTED",
        )
    )

    # Productos limpieza
    limpieza_products = [
        ("Lavandina 5L",             "LAV-001", "lavandinas",    Decimal("1800"), Decimal("1100"), 40,  8),
        ("Detergente Magistral 750ml","DET-001", "detergentes",  Decimal("1200"), Decimal("720"),  60, 15),
        ("Desinfectante Lysoform 1L", "DES-001", "desinfectantes",Decimal("2200"),Decimal("1400"), 30,  8),
        ("Limpiador multiuso Flash",  "LIM-001", "limpiadores",  Decimal("980"),  Decimal("580"),  45, 12),
        ("Bolsas basura x10 50L",     "BOL-001", "descartables", Decimal("650"),  Decimal("380"),  80, 20),
        ("Escoba plástica",           "ACC-001", "accesorios",   Decimal("1500"), Decimal("900"),  20,  5),
        ("Trapo de piso x3",          "ACC-002", "accesorios",   Decimal("800"),  Decimal("480"),  35, 10),
        ("Jabón en polvo 500g",       "JAB-001", "jabones",      Decimal("1100"), Decimal("650"),  50, 12),
    ]
    for name, sku, cat, price, cost, stock, threshold in limpieza_products:
        session.add(
            Product(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                name=name,
                sku=sku,
                category=cat,
                sale_price_ars=price,
                unit_cost_ars=cost,
                stock_units=stock,
                low_stock_threshold_units=threshold,
                is_active=True,
            )
        )

    # Ventas 30 días
    for i in range(30):
        sale_date = TODAY - timedelta(days=i)
        amount = Decimal("9300") if sale_date.weekday() < 5 else Decimal("5500")
        session.add(
            SaleEntry(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                amount=amount,
                quantity=1,
                transaction_date=sale_date,
                payment_method="transfer",
                notes="demo",
            )
        )

    for cat, amount, recurring, desc, method, days_ago in [
        ("rent",     Decimal("320000"), True,  "Alquiler depósito",       "transfer", 1),
        ("supplies", Decimal("2100000"),False, "Compra mercadería",       "transfer", 4),
        ("utilities",Decimal("18000"),  True,  "Electricidad y agua",     "transfer", 6),
        ("misc",     Decimal("25000"),  False, "Fletes y distribución",   "cash",     10),
    ]:
        session.add(
            ExpenseEntry(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                amount=amount,
                category=cat,
                transaction_date=TODAY - timedelta(days=days_ago),
                description=desc,
                is_recurring=recurring,
                payment_method=method,
            )
        )

    # 3 notificaciones — 2 críticas
    for title, body, ntype in [
        (
            "⚠️ Caja crítica: 17 días de cobertura",
            "Tu caja cubre solo 17 días de gastos fijos. El mínimo recomendado es 30 días. Tomá acción ahora.",
            "ALERT",
        ),
        (
            "⚠️ Proveedor único: riesgo muy alto",
            "Dependés de un solo proveedor para todo tu stock. Un corte de abastecimiento puede parar tu negocio.",
            "ALERT",
        ),
        (
            "Tu score bajó 12 puntos en el último mes",
            "Tu score pasó de 63 a 51. El riesgo principal es la caja. Revisá las acciones sugeridas.",
            "SCORE_DOWN",
        ),
    ]:
        session.add(
            Notification(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                user_id=user_id,
                title=title,
                body=body,
                notification_type=ntype,
                channel="in_app",
                is_read=False,
            )
        )

    await session.commit()
    print(f"  [limpieza] {email} → score 51 | declining | CASH_LOW crítico")


async def _seed_deco(session: AsyncSession) -> None:
    """Casa & Deco Palermo — Score 62, estable, stock inmovilizado, margen bajo."""
    from app.persistence.models.business import (  # noqa: PLC0415
        ActionSuggestion,
        BusinessProfile,
        BusinessSnapshot,
        Insight,
        MomentumProfile,
    )
    from app.persistence.models.notification import Notification  # noqa: PLC0415
    from app.persistence.models.product import Product  # noqa: PLC0415
    from app.persistence.models.score import (  # noqa: PLC0415
        HealthScoreSnapshot,
        WeeklyScoreHistory,
    )
    from app.persistence.models.tenant import Subscription, Tenant  # noqa: PLC0415
    from app.persistence.models.transaction import ExpenseEntry, SaleEntry  # noqa: PLC0415
    from app.persistence.models.user import User  # noqa: PLC0415

    tenant_id = uuid.uuid4()
    email = "demo.deco@vektor.app"

    session.add(
        Tenant(
            tenant_id=tenant_id,
            legal_name="Casa & Deco Palermo SRL",
            display_name="Casa & Deco Palermo",
            currency="ARS",
            pricing_reference_mode="MEP",
            status="ACTIVE",
        )
    )
    await session.flush()
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
    user_id = uuid.uuid4()
    session.add(
        User(
            user_id=user_id,
            tenant_id=tenant_id,
            email=email,
            password_hash=_pwd.hash(DEMO_PASSWORD),
            full_name="Valentina Torres",
            role_code="OWNER",
            is_active=True,
        )
    )
    session.add(
        BusinessProfile(
            profile_id=uuid.uuid4(),
            tenant_id=tenant_id,
            vertical_code="decoracion_hogar",
            data_mode="M0",
            data_confidence="LOW",
            monthly_sales_estimate_ars=Decimal("1800000"),
            monthly_inventory_spend_estimate_ars=Decimal("980000"),
            monthly_fixed_expenses_estimate_ars=Decimal("450000"),
            cash_on_hand_estimate_ars=Decimal("580000"),
            supplier_count_estimate=4,
            product_count_estimate=320,
            onboarding_completed=True,
            heuristic_profile_version="v1",
        )
    )
    snap_id = uuid.uuid4()
    session.add(
        BusinessSnapshot(
            id=snap_id,
            tenant_id=tenant_id,
            snapshot_date=NOW,
            snapshot_version="v1",
            data_completeness_score=Decimal("55.00"),
            data_mode="M0",
            confidence_level="LOW",
            raw_inputs_json={
                "monthly_sales": 1800000,
                "monthly_expenses": 1430000,
                "cash_on_hand": 580000,
                "supplier_count": 4,
            },
            created_at=NOW,
        )
    )

    # 8 semanas stable con leve recuperación: 67→65→63→62→61→61→62→62
    weekly_scores = [67, 65, 63, 62, 61, 61, 62, 62]
    latest_score_id = uuid.uuid4()

    for i, score in enumerate(weekly_scores):
        weeks_back = len(weekly_scores) - i
        snap_date = datetime.combine(
            _weeks_ago(weeks_back), datetime.min.time(), tzinfo=timezone.utc
        )
        sid = latest_score_id if weeks_back == 1 else uuid.uuid4()
        session.add(
            HealthScoreSnapshot(
                id=sid,
                tenant_id=tenant_id,
                total_score=Decimal(str(score)),
                level=_score_level(score),
                dimensions={
                    "cash":     {"score": 72 if weeks_back == 1 else min(80, score + 10), "label": _score_level(score)},
                    "margin":   {"score": 68 if weeks_back == 1 else score + 6, "label": _score_level(score)},
                    "stock":    {"score": 48 if weeks_back == 1 else max(40, score - 14), "label": "MEDIUM"},
                    "supplier": {"score": 78 if weeks_back == 1 else min(80, score + 16), "label": "GOOD"},
                },
                triggered_by="seed_demo",
                snapshot_date=snap_date,
                created_at=snap_date,
                score_cash=72 if weeks_back == 1 else min(80, score + 10),
                score_margin=68 if weeks_back == 1 else score + 6,
                score_stock=48 if weeks_back == 1 else max(40, score - 14),
                score_supplier=78 if weeks_back == 1 else min(80, score + 16),
                source_snapshot_id=snap_id if weeks_back == 1 else None,
                heuristic_version="v1",
                primary_risk_code="MARGIN_LOW",
                confidence_level="LOW",
                data_completeness_score=Decimal("55.00"),
            )
        )

    for i, score in enumerate(weekly_scores):
        weeks_back = len(weekly_scores) - i
        w_start = _weeks_ago(weeks_back)
        prev_score = weekly_scores[i - 1] if i > 0 else None
        delta = Decimal(str(score - prev_score)) if prev_score is not None else None
        if delta is None:
            trend = None
        elif delta > 0:
            trend = "IMPROVING"
        elif delta < 0:
            trend = "DECLINING"
        else:
            trend = "STABLE"
        session.add(
            WeeklyScoreHistory(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                week_start=w_start,
                week_end=w_start + timedelta(days=6),
                avg_score=Decimal(str(score)),
                min_score=Decimal(str(score - 2)),
                max_score=Decimal(str(score + 2)),
                level=_score_level(score),
                created_at=datetime.combine(w_start, datetime.min.time(), tzinfo=timezone.utc),
                delta=delta,
                trend_label=trend,
            )
        )

    session.add(
        MomentumProfile(
            tenant_id=tenant_id,
            best_score_ever=67,
            best_score_date=_weeks_ago(8),
            improving_streak_weeks=1,
            estimated_value_protected_ars=Decimal("95000.00"),
            milestones_json=[
                {
                    "id": "M1",
                    "label": "Primera semana de mejora",
                    "unlocked": True,
                    "unlocked_at": TODAY.isoformat(),
                },
            ],
            active_goal_json={"target_score": 70, "deadline_weeks": 8},
            updated_at=NOW,
        )
    )

    margin_insight_id = uuid.uuid4()
    session.add(
        Insight(
            id=margin_insight_id,
            tenant_id=tenant_id,
            insight_type="MARGIN_LOW",
            title="Margen por debajo del benchmark del rubro",
            description=(
                "Tu margen neto es 20.6%, por debajo del rango saludable del rubro (25%-45%). "
                "Revisá precios y reducí descuentos para recuperar rentabilidad."
            ),
            severity_code="MEDIUM",
            heuristic_version="v1",
        )
    )
    await session.flush()
    session.add(
        ActionSuggestion(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            insight_id=margin_insight_id,
            action_type="REVIEW_PRICING",
            title="Revisá precios en las 20 categorías de mayor rotación",
            description=(
                "Un ajuste del 8-12% en tus productos de mayor salida puede llevar "
                "el margen al 26% sin impacto significativo en el volumen de ventas."
            ),
            risk_level="LOW",
            status="SUGGESTED",
        )
    )

    stock_insight_id = uuid.uuid4()
    session.add(
        Insight(
            id=stock_insight_id,
            tenant_id=tenant_id,
            insight_type="STOCK_IMMOBILIZED",
            title="40% del catálogo sin rotación en más de 120 días",
            description=(
                "128 productos no registraron ventas en los últimos 4 meses. "
                "Ese stock inmovilizado representa capital que no trabaja para tu negocio."
            ),
            severity_code="MEDIUM",
            heuristic_version="v1",
        )
    )
    await session.flush()
    session.add(
        ActionSuggestion(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            insight_id=stock_insight_id,
            action_type="RUN_CLEARANCE_SALE",
            title="Liquidá el stock sin movimiento con un 20% de descuento",
            description=(
                "Identificá los 30 productos sin venta en 90+ días y armá una promoción. "
                "Recuperar ese capital puede mejorar tu score de stock en hasta 10 puntos."
            ),
            risk_level="LOW",
            status="SUGGESTED",
        )
    )

    # Productos decoración
    deco_products = [
        ("Maceta cerámica 20cm",       "MAC-001", "macetas",     Decimal("3500"),  Decimal("1800"), 25, 5),
        ("Portarretrato madera 10x15", "DEC-001", "decoracion",  Decimal("2200"),  Decimal("1100"), 40, 8),
        ("Vela aromática soja",        "VEL-001", "velas",       Decimal("1800"),  Decimal("800"),  60, 15),
        ("Cojín decorativo 45x45",     "COJ-001", "textiles",    Decimal("4500"),  Decimal("2200"), 18, 4),
        ("Espejo redondo 50cm",        "ESP-001", "espejos",     Decimal("8900"),  Decimal("4200"), 10, 3),
        ("Mantel de lino 150x200",     "MAN-001", "textiles",    Decimal("6200"),  Decimal("3100"), 15, 4),
        ("Florero vidrio soplado",     "FLO-001", "floreros",    Decimal("4800"),  Decimal("2400"), 22, 5),
        ("Lampara velador E27",        "LAM-001", "iluminacion", Decimal("7500"),  Decimal("3800"), 12, 3),
        ("Canasta mimbre pequeña",     "CAN-001", "canastas",    Decimal("2800"),  Decimal("1400"), 30, 8),
        ("Cuadro canvas 40x60",        "CUA-001", "cuadros",     Decimal("5500"),  Decimal("2700"), 20, 5),
    ]
    for name, sku, cat, price, cost, stock, threshold in deco_products:
        session.add(
            Product(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                name=name,
                sku=sku,
                category=cat,
                sale_price_ars=price,
                unit_cost_ars=cost,
                stock_units=stock,
                low_stock_threshold_units=threshold,
                is_active=True,
            )
        )

    # Ventas 30 días
    for i in range(30):
        sale_date = TODAY - timedelta(days=i)
        # Deco tiene picos fin de semana
        amount = Decimal("5500") if sale_date.weekday() < 5 else Decimal("9200")
        session.add(
            SaleEntry(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                amount=amount,
                quantity=1,
                transaction_date=sale_date,
                payment_method="card",
                notes="demo",
            )
        )

    for cat, amount, recurring, desc, method, days_ago in [
        ("rent",     Decimal("450000"), True,  "Alquiler local Palermo",   "transfer", 1),
        ("supplies", Decimal("980000"), False, "Compra de colección",      "transfer", 5),
        ("utilities",Decimal("28000"),  True,  "Servicios del local",      "transfer", 7),
        ("marketing",Decimal("45000"),  False, "Instagram y publicidad",   "transfer", 10),
        ("misc",     Decimal("18000"),  False, "Materiales y embalaje",    "cash",     14),
    ]:
        session.add(
            ExpenseEntry(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                amount=amount,
                category=cat,
                transaction_date=TODAY - timedelta(days=days_ago),
                description=desc,
                is_recurring=recurring,
                payment_method=method,
            )
        )

    # 3 notificaciones
    for title, body, ntype in [
        (
            "40% del catálogo sin rotación",
            "128 productos sin ventas en 120+ días. Ese stock inmovilizado representa capital que no trabaja.",
            "INSIGHT",
        ),
        (
            "Margen por debajo del benchmark",
            "Tu margen del 20.6% está por debajo del mínimo saludable para el rubro (25%). Revisá precios.",
            "INSIGHT",
        ),
        (
            "Primera semana de mejora detectada",
            "Tu score subió de 61 a 62. ¡Seguí así y desbloqueás el hito M2 en 2 semanas!",
            "MILESTONE",
        ),
    ]:
        session.add(
            Notification(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                user_id=user_id,
                title=title,
                body=body,
                notification_type=ntype,
                channel="in_app",
                is_read=False,
            )
        )

    await session.commit()
    print(f"  [deco]     {email} → score 62 | stable | MARGIN_LOW + STOCK_IMMOBILIZED")


# ── Entry points ──────────────────────────────────────────────────────────────


async def main(reset: bool = False) -> None:
    engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as session:
        if reset:
            print("Resetting demo tenants...")
            await _drop_demo_tenants(session)

        # Check which tenants already exist
        existing = []
        for email in DEMO_TENANTS:
            tid = await _find_tenant_by_email(session, email)
            if tid:
                existing.append(email)

        if existing and not reset:
            print("Demo tenants already exist:")
            for e in existing:
                print(f"  {e}")
            print("Use 'make reset-demo' to re-seed.")
            await engine.dispose()
            return

        print("Seeding demo tenants...")
        await _seed_kiosco(session)
        await _seed_limpieza(session)
        await _seed_deco(session)

    await engine.dispose()
    print("\nDone. Credentials (all use password: Demo1234!):")
    print("  demo.kiosco@vektor.app  — Kiosco San Martín   (score 74)")
    print("  demo.limpieza@vektor.app — Distribuidora Clean  (score 51)")
    print("  demo.deco@vektor.app    — Casa & Deco Palermo  (score 62)")


if __name__ == "__main__":
    reset_flag = "--reset" in sys.argv
    asyncio.run(main(reset=reset_flag))
