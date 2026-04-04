"""
Suite de integración para Véktor — 7 escenarios de negocio.

Cada test es independiente: crea sus propios datos, ejecuta y verifica.
No hay dependencia de orden ni de estado compartido entre tests.

Cobertura
---------
  S1 - Kiosco saludable            score_total > 65, primary_risk ≠ CASH_LOW
  S2 - Kiosco riesgo caja          primary_risk = CASH_LOW, score_cash < 30
  S3 - Deco hogar margen bajo      primary_risk = MARGIN_LOW
  S4 - Proveedor único             score_supplier < 45, primary_risk = SUPPLIER_DEPENDENCY
  S5 - Tenant isolation            datos de A inaccesibles con token de B (y viceversa)
  S6 - End-to-end HTTP             register → login → onboarding → job → GET /latest
  S7 - Momentum dos semanas        2 entradas en weekly_score_history, active_goal correcto

Fórmula del health engine (health_engine.py):
    score_total = cash*0.30 + margin*0.30 + stock*0.25 + supplier*0.15

Lógica cash:
    cash_ratio = cash_on_hand / monthly_fixed_expenses
    Bands: [0,0.3)→[0,14], [0.3,0.7)→[15,39], [0.7,1.2)→[40,69],
           [1.2,2.0)→[70,89], [2.0,4.0)→[90,100]

Lógica margin (kiosco benchmark: critical<0.10, warning<0.18, healthy 0.18–0.28):
    margin = (ventas - inventario - gastos_fijos) / ventas

Supplier bands: count=1→15, count=2→[15,44], count=3→70, count≥4→85+
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobs.update_momentum import _current_week, run_momentum_update
from app.persistence.models.business import MomentumProfile
from app.persistence.models.product import Product
from app.persistence.models.score import HealthScoreSnapshot, WeeklyScoreHistory

from .conftest import (
    FakeRedis,
    make_auth_headers,
    make_profile,
    make_tenant,
    make_user,
    run_pipeline,
)


# ══════════════════════════════════════════════════════════════════════════════
# SCENARIO 1 — Kiosco saludable
#
# ventas:    350 000 ARS/semana → 1 400 000 ARS/mes
# mercadería: 180 000 ARS/mes
# gastos fijos: 80 000 ARS/mes
# caja:      150 000 ARS
# proveedores: 3   productos (estimado): 45
#
# cash_ratio = 150k / 80k = 1.875 → band [1.2, 2.0) → score_cash ≈ 86
# margin     = (1.4M - 180k - 80k) / 1.4M = 81.4% → above kiosco max (28%) → 100
# supplier   = 3 → 70   stock (sin productos reales) → 50
# total      = round(86*0.30 + 100*0.30 + 50*0.25 + 70*0.15) = round(78.8) = 79
# primary_risk = stock(50) → STOCK_CRITICAL   ≠ CASH_LOW ✓
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_s1_kiosco_saludable(session: AsyncSession, fake_redis: FakeRedis) -> None:
    # ── Setup ─────────────────────────────────────────────────────────────────
    tenant = await make_tenant(session, legal_name="Kiosco El Rápido")
    await make_profile(
        session,
        tenant.tenant_id,
        "kiosco",
        monthly_sales=Decimal("1400000"),
        monthly_inventory=Decimal("180000"),
        monthly_fixed=Decimal("80000"),
        cash_on_hand=Decimal("150000"),
        supplier_count=3,
        product_count=45,
    )

    # ── Execute ───────────────────────────────────────────────────────────────
    snap = await run_pipeline(session, fake_redis, tenant.tenant_id)

    # ── Assert ────────────────────────────────────────────────────────────────
    assert int(snap.total_score) > 65, (
        f"score_total esperado > 65, obtenido {snap.total_score}"
    )
    assert snap.primary_risk_code != "CASH_LOW", (
        f"primary_risk no debe ser CASH_LOW, obtenido {snap.primary_risk_code!r}"
    )
    # score_cash debe reflejar la buena liquidez (ratio 1.875)
    assert snap.score_cash is not None and snap.score_cash > 65, (
        f"score_cash esperado > 65, obtenido {snap.score_cash}"
    )
    assert snap.confidence_level in ("HIGH", "MEDIUM", "LOW")
    assert snap.heuristic_version == "v1"


# ══════════════════════════════════════════════════════════════════════════════
# SCENARIO 2 — Kiosco en riesgo de caja
#
# ventas:    200 000 ARS/semana → 800 000 ARS/mes
# mercadería: 160 000 ARS/mes
# gastos fijos: 90 000 ARS/mes
# caja:       40 000 ARS          ← cash_ratio = 40k/90k = 0.444 → CRÍTICO
# proveedores: 3
#
# cash_ratio = 0.444 → band [0.3, 0.7) → score_cash = int(15 + 0.36*24) = 23
# margin     = (800k - 160k - 90k) / 800k = 68.75% → 100
# supplier   = 70   stock = 50
# total      = round(23*0.30 + 100*0.30 + 50*0.25 + 70*0.15) = round(59.9) = 60
# primary_risk = cash(23) → CASH_LOW ✓   score_cash = 23 < 30 ✓
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_s2_kiosco_riesgo_caja(session: AsyncSession, fake_redis: FakeRedis) -> None:
    # ── Setup ─────────────────────────────────────────────────────────────────
    tenant = await make_tenant(session, legal_name="Kiosco Ajustado")
    await make_profile(
        session,
        tenant.tenant_id,
        "kiosco",
        monthly_sales=Decimal("800000"),
        monthly_inventory=Decimal("160000"),
        monthly_fixed=Decimal("90000"),
        cash_on_hand=Decimal("40000"),
        supplier_count=3,
    )

    # ── Execute ───────────────────────────────────────────────────────────────
    snap = await run_pipeline(session, fake_redis, tenant.tenant_id)

    # ── Assert ────────────────────────────────────────────────────────────────
    assert snap.primary_risk_code == "CASH_LOW", (
        f"primary_risk_code esperado 'CASH_LOW', obtenido {snap.primary_risk_code!r}"
    )
    assert snap.score_cash is not None and snap.score_cash < 30, (
        f"score_cash esperado < 30 (crítico), obtenido {snap.score_cash}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# SCENARIO 3 — Decoración hogar: margen bajo
#
# ventas:    2 000 000 ARS/mes
# mercadería: 1 400 000 ARS/mes
# gastos fijos: 250 000 ARS/mes
# caja:        300 000 ARS
# proveedores: 3
#
# margin = (2M - 1.4M - 250k) / 2M = 350k/2M = 17.5%
# deco_benchmark: critical_below=0.15, warning_below=0.30
#   → band [0.15, 0.30) → score_margin = int(15 + 0.1667*24) = 19
# cash_ratio = 300k/250k = 1.2 → band boundary → score_cash = 70
# primary_risk = min(70, 19, 50, 70) = margin(19) → MARGIN_LOW ✓
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_s3_deco_hogar_margen_bajo(
    session: AsyncSession, fake_redis: FakeRedis
) -> None:
    # ── Setup ─────────────────────────────────────────────────────────────────
    tenant = await make_tenant(session, legal_name="Deco Hogar Sur")
    await make_profile(
        session,
        tenant.tenant_id,
        "decoracion_hogar",
        monthly_sales=Decimal("2000000"),
        monthly_inventory=Decimal("1400000"),
        monthly_fixed=Decimal("250000"),
        cash_on_hand=Decimal("300000"),
        supplier_count=3,
    )

    # ── Execute ───────────────────────────────────────────────────────────────
    snap = await run_pipeline(session, fake_redis, tenant.tenant_id)

    # ── Assert ────────────────────────────────────────────────────────────────
    assert snap.primary_risk_code == "MARGIN_LOW", (
        f"primary_risk_code esperado 'MARGIN_LOW', obtenido {snap.primary_risk_code!r}"
    )
    assert snap.score_margin is not None and snap.score_margin < 40, (
        f"score_margin esperado < 40 (warning zone), obtenido {snap.score_margin}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# SCENARIO 4 — Proveedor único
#
# vertical: kiosco   supplier_count = 1
# ventas:   700 000 ARS/mes  inv: 100 000  fixed: 50 000  cash: 200 000
#
# supplier_score = _score_supplier(1) = 15 (value=1.0 ≤ low=1 → s_low=15)
# cash_ratio = 200k/50k = 4.0 → above all bands → 100
# margin     = (700k - 100k - 50k) / 700k = 78.6% → above kiosco max → 100
# stock      = 50 (neutral)
# total      = round(100*0.30 + 100*0.30 + 50*0.25 + 15*0.15) ≈ 75
# primary_risk = min(100, 100, 50, 15) = supplier(15) → SUPPLIER_DEPENDENCY ✓
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_s4_proveedor_unico(session: AsyncSession, fake_redis: FakeRedis) -> None:
    # ── Setup ─────────────────────────────────────────────────────────────────
    tenant = await make_tenant(session, legal_name="Kiosco Un Solo Proveedor")
    await make_profile(
        session,
        tenant.tenant_id,
        "kiosco",
        monthly_sales=Decimal("700000"),
        monthly_inventory=Decimal("100000"),
        monthly_fixed=Decimal("50000"),
        cash_on_hand=Decimal("200000"),
        supplier_count=1,
    )

    # ── Execute ───────────────────────────────────────────────────────────────
    snap = await run_pipeline(session, fake_redis, tenant.tenant_id)

    # ── Assert ────────────────────────────────────────────────────────────────
    assert snap.score_supplier is not None and snap.score_supplier < 45, (
        f"score_supplier esperado < 45, obtenido {snap.score_supplier}"
    )
    assert snap.primary_risk_code == "SUPPLIER_DEPENDENCY", (
        f"primary_risk_code esperado 'SUPPLIER_DEPENDENCY', obtenido {snap.primary_risk_code!r}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# SCENARIO 5 — Tenant isolation
#
# Dos tenants con scores y productos propios.
# Verificaciones:
#   a) GET /health-scores/latest con token de A retorna snapshot de A, no de B.
#   b) GET /products con token de A no incluye productos de B.
#   c) GET /products con token de B no incluye productos de A.
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_s5_tenant_isolation(
    session: AsyncSession,
    fake_redis: FakeRedis,
    client: AsyncClient,
) -> None:
    # ── Setup Tenant A (kiosco) ───────────────────────────────────────────────
    tenant_a = await make_tenant(session, legal_name="Tenant A — Kiosco")
    user_a = await make_user(session, tenant_a.tenant_id, email="owner@tenant-a.com")
    await make_profile(
        session,
        tenant_a.tenant_id,
        "kiosco",
        monthly_sales=Decimal("1000000"),
        monthly_inventory=Decimal("200000"),
        monthly_fixed=Decimal("80000"),
        cash_on_hand=Decimal("100000"),
        supplier_count=2,
    )
    snap_a = await run_pipeline(session, fake_redis, tenant_a.tenant_id)

    product_a = Product(
        tenant_id=tenant_a.tenant_id,
        name="Gaseosa Tenant A",
        sale_price_ars=Decimal("500.00"),
        unit_cost_ars=Decimal("250.00"),
        stock_units=100,
        low_stock_threshold_units=10,
        is_active=True,
    )
    session.add(product_a)

    # ── Setup Tenant B (limpieza) ─────────────────────────────────────────────
    tenant_b = await make_tenant(session, legal_name="Tenant B — Limpieza")
    user_b = await make_user(session, tenant_b.tenant_id, email="owner@tenant-b.com")
    await make_profile(
        session,
        tenant_b.tenant_id,
        "limpieza",
        monthly_sales=Decimal("500000"),
        monthly_inventory=Decimal("100000"),
        monthly_fixed=Decimal("50000"),
        cash_on_hand=Decimal("80000"),
        supplier_count=2,
    )
    snap_b = await run_pipeline(session, fake_redis, tenant_b.tenant_id)

    product_b = Product(
        tenant_id=tenant_b.tenant_id,
        name="Lavandina Tenant B",
        sale_price_ars=Decimal("300.00"),
        unit_cost_ars=Decimal("150.00"),
        stock_units=50,
        low_stock_threshold_units=5,
        is_active=True,
    )
    session.add(product_b)
    await session.commit()

    headers_a = make_auth_headers(user_a.user_id, tenant_a.tenant_id)
    headers_b = make_auth_headers(user_b.user_id, tenant_b.tenant_id)

    # ── a) Health-score isolation ─────────────────────────────────────────────
    r_a = await client.get("/api/v1/health-scores/latest", headers=headers_a)
    assert r_a.status_code == 200, r_a.text
    data_a = r_a.json()

    r_b = await client.get("/api/v1/health-scores/latest", headers=headers_b)
    assert r_b.status_code == 200, r_b.text
    data_b = r_b.json()

    # Ambos tenants tienen scores — no deben retornar CALCULATING
    assert "score_total" in data_a, f"Tenant A debe tener score, obtenido: {data_a}"
    assert "score_total" in data_b, f"Tenant B debe tener score, obtenido: {data_b}"

    # Los snapshot IDs deben ser distintos (cada tenant ve su propio score)
    assert data_a["id"] != data_b["id"], (
        "Tenant A y Tenant B retornaron el mismo snapshot — violación de aislación"
    )
    # El tenant_id en la respuesta debe coincidir con el tenant autenticado
    assert data_a["tenant_id"] == str(tenant_a.tenant_id)
    assert data_b["tenant_id"] == str(tenant_b.tenant_id)

    # ── b) Product isolation: Tenant A no ve productos de B ───────────────────
    r = await client.get("/api/v1/products", headers=headers_a)
    assert r.status_code == 200, r.text
    product_ids_a = {p["id"] for p in r.json()}
    assert str(product_b.id) not in product_ids_a, (
        "Tenant A no debe ver productos de Tenant B"
    )

    # ── c) Product isolation: Tenant B no ve productos de A ───────────────────
    r = await client.get("/api/v1/products", headers=headers_b)
    assert r.status_code == 200, r.text
    product_ids_b = {p["id"] for p in r.json()}
    assert str(product_a.id) not in product_ids_b, (
        "Tenant B no debe ver productos de Tenant A"
    )


# ══════════════════════════════════════════════════════════════════════════════
# SCENARIO 6 — Flujo completo end-to-end
#
# register → login → onboarding/submit → run_pipeline (job simulado) → GET /latest
# Verifica que el score existe y tiene confidence_level.
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_s6_end_to_end(
    session: AsyncSession,
    fake_redis: FakeRedis,
    client: AsyncClient,
) -> None:
    # ── 1. Register ───────────────────────────────────────────────────────────
    r = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "e2e@kiosco.com",
            "password": "Secure123",
            "full_name": "E2E Test Owner",
            "business_name": "Kiosco E2E",
            "vertical_code": "kiosco",
        },
    )
    assert r.status_code == 201, r.text

    # ── 2. Login ──────────────────────────────────────────────────────────────
    r = await client.post(
        "/api/v1/auth/login",
        json={"email": "e2e@kiosco.com", "password": "Secure123"},
    )
    assert r.status_code == 200, r.text
    login_data = r.json()
    tenant_id = uuid.UUID(login_data["user"]["tenant_id"])
    headers = {"Authorization": f"Bearer {login_data['access_token']}"}

    # ── 3. Onboarding (mock Celery trigger — no Redis needed) ─────────────────
    with patch("app.jobs.score_worker.trigger_score_recalculation") as mock_task:
        mock_task.delay = lambda *a, **kw: None
        r = await client.post(
            "/api/v1/onboarding/submit",
            json={
                "vertical_code": "kiosco",
                "weekly_sales_estimate_ars": "350000",
                "monthly_inventory_cost_ars": "180000",
                "monthly_fixed_expenses_ars": "80000",
                "cash_on_hand_ars": "150000",
                "product_count_estimate": 45,
                "supplier_count_estimate": 3,
                "main_concern": "CASH",
            },
            headers=headers,
        )
    assert r.status_code in (200, 201), r.text

    # ── 4. Run pipeline (simulates Celery job synchronously) ──────────────────
    await run_pipeline(session, fake_redis, tenant_id)

    # ── 5. GET /health-scores/latest ──────────────────────────────────────────
    r = await client.get("/api/v1/health-scores/latest", headers=headers)
    assert r.status_code == 200, r.text
    data = r.json()

    assert "score_total" in data, (
        f"Esperado score_total en respuesta, obtenido: {data}"
    )
    assert "status" not in data, (
        "No debe retornar {status: CALCULATING} si el score ya fue calculado"
    )
    assert "confidence_level" in data, (
        f"Esperado confidence_level en respuesta, obtenido: {data}"
    )
    assert 0 <= data["score_total"] <= 100, (
        f"score_total fuera de rango: {data['score_total']}"
    )
    assert data["confidence_level"] in ("HIGH", "MEDIUM", "LOW"), (
        f"confidence_level inválido: {data['confidence_level']!r}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# SCENARIO 7 — Momentum después de 2 semanas
#
# Ciclo 1: score semana anterior — insertado directamente en DB
#          (simula que ya corrió update_momentum_profile la semana pasada).
# Ciclo 2: score semana actual + run_momentum_update() en vivo.
#
# Verificaciones:
#   a) weekly_score_history tiene exactamente 2 entradas.
#   b) active_goal.weak_dimension apunta al subscore más bajo (margin=25 < todos).
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_s7_momentum_dos_semanas(
    session: AsyncSession, fake_redis: FakeRedis
) -> None:
    # ── Setup tenant ──────────────────────────────────────────────────────────
    tenant = await make_tenant(session, legal_name="Kiosco Momentum")
    await make_profile(
        session,
        tenant.tenant_id,
        "kiosco",
        monthly_sales=Decimal("800000"),
        monthly_inventory=Decimal("150000"),
        monthly_fixed=Decimal("70000"),
        cash_on_hand=Decimal("100000"),
        supplier_count=3,
    )

    # ── Ciclo 1: datos de la semana anterior ──────────────────────────────────
    week_start_cur, week_end_cur = _current_week()
    week_start_prev = week_start_cur - timedelta(days=7)
    week_end_prev = week_start_cur - timedelta(days=1)

    # HealthScoreSnapshot creado en el lunes de la semana anterior
    prev_created_at = datetime(
        week_start_prev.year,
        week_start_prev.month,
        week_start_prev.day,
        12, 0, 0,
        tzinfo=UTC,
    )
    prev_snap = HealthScoreSnapshot(
        id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        total_score=Decimal("60"),
        level="good",
        dimensions={},
        triggered_by="test:cycle1",
        snapshot_date=prev_created_at,
        created_at=prev_created_at,
        # subscores: cash=70, margin=30, stock=60, supplier=70
        score_cash=70,
        score_margin=30,
        score_stock=60,
        score_supplier=70,
        primary_risk_code="MARGIN_LOW",
        confidence_level="MEDIUM",
        data_completeness_score=Decimal("80"),
        heuristic_version="v1",
    )
    session.add(prev_snap)

    # WeeklyScoreHistory de la semana anterior (resultado del ciclo 1)
    prev_week_row = WeeklyScoreHistory(
        tenant_id=tenant.tenant_id,
        week_start=week_start_prev,
        week_end=week_end_prev,
        avg_score=Decimal("60"),
        min_score=Decimal("60"),
        max_score=Decimal("60"),
        level="good",
        delta=Decimal("0"),
        trend_label="stable",
        created_at=prev_created_at,
    )
    session.add(prev_week_row)
    await session.commit()

    # ── Ciclo 2: score de la semana actual ────────────────────────────────────
    # score_margin=25 sigue siendo el subscore más bajo → active_goal = margin
    cur_created_at = datetime.now(UTC)
    cur_snap = HealthScoreSnapshot(
        id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        total_score=Decimal("65"),
        level="good",
        dimensions={},
        triggered_by="test:cycle2",
        snapshot_date=cur_created_at,
        created_at=cur_created_at,
        score_cash=80,
        score_margin=25,   # ← mínimo → active_goal debe apuntar a "margin"
        score_stock=60,
        score_supplier=70,
        primary_risk_code="MARGIN_LOW",
        confidence_level="MEDIUM",
        data_completeness_score=Decimal("80"),
        heuristic_version="v1",
    )
    session.add(cur_snap)
    await session.commit()

    # ── Execute ciclo 2 ───────────────────────────────────────────────────────
    await run_momentum_update(tenant.tenant_id, session)

    # ── Assert a) 2 entradas en weekly_score_history ──────────────────────────
    result = await session.execute(
        select(WeeklyScoreHistory)
        .where(WeeklyScoreHistory.tenant_id == tenant.tenant_id)
        .order_by(WeeklyScoreHistory.week_start)
    )
    rows = result.scalars().all()
    assert len(rows) == 2, (
        f"Esperado 2 entradas en weekly_score_history, encontrado {len(rows)}"
    )
    assert rows[0].week_start == week_start_prev, "Primera entrada debe ser semana anterior"
    assert rows[1].week_start == week_start_cur, "Segunda entrada debe ser semana actual"

    # La semana actual debe mostrar delta positivo (60 → 65 = +5)
    assert rows[1].delta is not None and float(rows[1].delta) > 0, (
        f"delta esperado > 0 (mejora), obtenido {rows[1].delta}"
    )

    # ── Assert b) active_goal apunta al subscore más bajo (margin=25) ─────────
    mp_result = await session.execute(
        select(MomentumProfile).where(MomentumProfile.tenant_id == tenant.tenant_id)
    )
    mp = mp_result.scalar_one()

    assert mp.active_goal_json is not None, "active_goal_json no debe ser None"
    assert mp.active_goal_json["weak_dimension"] == "margin", (
        f"active_goal.weak_dimension esperado 'margin' (score=25 es mínimo), "
        f"obtenido {mp.active_goal_json['weak_dimension']!r}"
    )
    assert "goal" in mp.active_goal_json, "active_goal debe tener campo 'goal'"
    assert "action" in mp.active_goal_json, "active_goal debe tener campo 'action'"


# ══════════════════════════════════════════════════════════════════════════════
# SCENARIO 8 — Celery task _run() directo
#
# Objetivo: cubrir el cuerpo de _run() (líneas 94–216) y las ramas extremas de
# _score_level ("excellent" ≥ 80, "critical" < 30) que los escenarios anteriores
# no activan en total_score.
#
# Estrategia:
#   - Crear un engine/session propios (aislados del fixture compartido).
#   - Mockear create_async_engine → mock engine (con dispose() como AsyncMock).
#   - Mockear sessionmaker → nuestra fresh_factory.
#   - Mockear Redis.from_url → FakeRedis.
#   - Llamar a _run(tenant_id) directamente; verificar snapshot persistido.
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_s8_celery_task_run() -> None:
    from unittest.mock import AsyncMock, MagicMock, patch

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from sqlalchemy.ext.asyncio import create_async_engine as _real_cae

    from app.jobs.recalculate_health_score import _run, _score_level
    from app.persistence.db.base import Base
    from app.persistence.models.score import HealthScoreSnapshot

    # ── Verify _score_level edge cases ────────────────────────────────────────
    assert _score_level(80) == "excellent"
    assert _score_level(100) == "excellent"
    assert _score_level(29) == "critical"
    assert _score_level(0) == "critical"

    # ── Isolated engine/session for this test ─────────────────────────────────
    fresh_engine = _real_cae("sqlite+aiosqlite:///:memory:", echo=False)
    async with fresh_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    fresh_factory = async_sessionmaker(
        fresh_engine, class_=AsyncSession, expire_on_commit=False
    )

    async with fresh_factory() as setup_session:
        tenant = await make_tenant(setup_session, legal_name="Celery Run Test")
        await make_profile(
            setup_session,
            tenant.tenant_id,
            "kiosco",
            monthly_sales=Decimal("900000"),
            monthly_inventory=Decimal("150000"),
            monthly_fixed=Decimal("60000"),
            cash_on_hand=Decimal("200000"),
        )

    # ── Mock infrastructure so _run() uses our fresh session ──────────────────
    fake_redis = FakeRedis()
    mock_engine = MagicMock()
    mock_engine.dispose = AsyncMock()

    with (
        patch("sqlalchemy.ext.asyncio.create_async_engine", return_value=mock_engine),
        patch("sqlalchemy.orm.sessionmaker", return_value=fresh_factory),
        patch("redis.asyncio.Redis") as mock_redis_cls,
        patch("app.config.settings.get_settings") as mock_get_settings,
        patch("app.jobs.generate_insight.generate_insight") as mock_insight,
    ):
        mock_get_settings.return_value = MagicMock(
            DATABASE_URL="sqlite+aiosqlite:///:memory:",
            REDIS_URL="redis://localhost",
        )
        mock_redis_cls.from_url.return_value = fake_redis
        mock_insight.delay = lambda *a, **kw: None

        await _run(str(tenant.tenant_id))

    # ── Verify snapshot was persisted ─────────────────────────────────────────
    async with fresh_factory() as verify_session:
        res = await verify_session.execute(
            select(HealthScoreSnapshot).where(
                HealthScoreSnapshot.tenant_id == tenant.tenant_id
            )
        )
        snap = res.scalar_one()
        assert snap.total_score > 0
        assert snap.heuristic_version == "v1"
        assert snap.triggered_by == "celery:recalculate_health_score"
        assert snap.primary_risk_code is not None

    await fresh_engine.dispose()
