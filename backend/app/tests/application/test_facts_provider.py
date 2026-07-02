"""Integración FactsProvider ↔ SQLite + reconciliación end-to-end de 90 días.

Complementa test_facts_reconciliation.py (unit, FakeProvider): acá se valida que
el MAPEO del ORM real al contrato canónico es correcto —

    - voided_at IS NULL (soft delete fuera)
    - provenance llega crudo (REAL/DEMO — filtra FactsService)
    - cost_ars derivado: Product.unit_cost_ars × quantity (NaN si falta)
    - last_sold_date derivado: max(transaction_date) por product_id (NaT si nunca)
    - borde intradía: func.date() sobre transaction_date DATETIME
    - payment_method="inflow" presente en el frame
    - PreloadedFactsProvider falla explícito fuera de cobertura
    - frames vacíos con columnas correctas → EMPTY, nunca $0 falso

— y la reconciliación de 90 días sintéticos (abril–junio 2026) contra totales
calculados a mano, que es LA compuerta del paso 2 antes de tocar consumidores.
"""

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pandas as pd
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.facts_provider import (
    PreloadedFactsProvider,
    build_facts_service,
    load_facts_frames,
)
from app.application.services.facts_service import Period, Provenance
from app.persistence.models.product import Product
from app.persistence.models.transaction import ExpenseEntry, SaleEntry

TODAY = date(2026, 6, 30)  # ancla determinista (misma que test_facts_reconciliation)
P90 = Period.last_n_days(90, today=TODAY)  # [2026-04-02, 2026-06-30]


def _dt(d: date, hour: int = 12, minute: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hour, minute)


def _sale(tenant_id: uuid.UUID, d: datetime, amount: float, method: str = "cash",
          **kw: object) -> SaleEntry:
    return SaleEntry(tenant_id=tenant_id, amount=Decimal(str(amount)),
                     transaction_date=d, payment_method=method, **kw)


def _expense(tenant_id: uuid.UUID, d: datetime, amount: float, category: str,
             expense_type: str, **kw: object) -> ExpenseEntry:
    return ExpenseEntry(tenant_id=tenant_id, amount=Decimal(str(amount)),
                        transaction_date=d, category=category,
                        expense_type=expense_type, description=f"gasto {category}", **kw)


# ─────────────────────────────────────────────────────────────────────────────
# Mapeo ORM → contrato canónico (tests focalizados)
# ─────────────────────────────────────────────────────────────────────────────
async def test_voided_excluida_del_frame(db_session: AsyncSession, sample_tenant) -> None:
    tid = sample_tenant.tenant_id
    db_session.add(_sale(tid, _dt(TODAY), 1000))
    db_session.add(_sale(tid, _dt(TODAY), 7000,
                         voided_at=datetime(2026, 6, 29, tzinfo=UTC),
                         void_reason="USER_CANCELLED"))
    await db_session.commit()

    frames = await load_facts_frames(db_session, tid, start=P90.start, end=P90.end)
    assert len(frames.sales) == 1
    assert frames.sales.iloc[0]["amount_ars"] == 1000.0


async def test_provenance_llega_crudo(db_session: AsyncSession, sample_tenant) -> None:
    tid = sample_tenant.tenant_id
    db_session.add(_sale(tid, _dt(TODAY), 1000, provenance="REAL"))
    db_session.add(_sale(tid, _dt(TODAY), 777, provenance="DEMO"))
    await db_session.commit()

    frames = await load_facts_frames(db_session, tid, start=P90.start, end=P90.end)
    # el provider NO filtra provenance — eso es responsabilidad de FactsService
    assert set(frames.sales["provenance"]) == {"REAL", "DEMO"}


async def test_cost_ars_derivado_de_producto(db_session: AsyncSession, sample_tenant) -> None:
    tid = sample_tenant.tenant_id
    con_costo = Product(tenant_id=tid, name="Coca 500", sale_price_ars=Decimal("1500"),
                        unit_cost_ars=Decimal("800"), stock_units=10)
    sin_costo = Product(tenant_id=tid, name="Alfajor", sale_price_ars=Decimal("900"),
                        unit_cost_ars=None, stock_units=5)
    db_session.add_all([con_costo, sin_costo])
    await db_session.flush()
    db_session.add(_sale(tid, _dt(TODAY), 4500, product_id=con_costo.id, quantity=3))
    db_session.add(_sale(tid, _dt(TODAY), 900, product_id=sin_costo.id))
    db_session.add(_sale(tid, _dt(TODAY), 1200))  # sin product_id
    await db_session.commit()

    frames = await load_facts_frames(db_session, tid, start=P90.start, end=P90.end)
    by_amount = {row["amount_ars"]: row for _, row in frames.sales.iterrows()}
    assert by_amount[4500.0]["cost_ars"] == 2400.0        # 800 × 3
    assert pd.isna(by_amount[900.0]["cost_ars"])          # producto sin unit_cost_ars
    assert pd.isna(by_amount[1200.0]["cost_ars"])         # venta sin producto


async def test_last_sold_date_derivado(db_session: AsyncSession, sample_tenant) -> None:
    tid = sample_tenant.tenant_id
    vendido = Product(tenant_id=tid, name="Coca 500", sale_price_ars=Decimal("1500"),
                      unit_cost_ars=Decimal("800"), stock_units=10)
    dormido = Product(tenant_id=tid, name="Lavandina", sale_price_ars=Decimal("700"),
                      unit_cost_ars=Decimal("500"), stock_units=50)
    db_session.add_all([vendido, dormido])
    await db_session.flush()
    db_session.add(_sale(tid, _dt(TODAY - timedelta(days=30)), 1500, product_id=vendido.id))
    db_session.add(_sale(tid, _dt(TODAY - timedelta(days=2)), 1500, product_id=vendido.id))
    await db_session.commit()

    frames = await load_facts_frames(db_session, tid, start=P90.start, end=P90.end)
    by_id = {row["product_id"]: row for _, row in frames.products.iterrows()}
    assert by_id[str(vendido.id)]["last_sold_date"] == _dt(TODAY - timedelta(days=2))  # el max
    assert pd.isna(by_id[str(dormido.id)]["last_sold_date"])  # nunca vendido → NaT


async def test_borde_intradia_func_date(db_session: AsyncSession, sample_tenant) -> None:
    tid = sample_tenant.tenant_id
    db_session.add(_sale(tid, _dt(TODAY, 23, 59), 1000))       # 30/06 23:59 → entra
    db_session.add(_sale(tid, _dt(TODAY + timedelta(days=1), 0, 0), 5000))  # 01/07 → fuera
    await db_session.commit()

    frames = await load_facts_frames(db_session, tid, start=P90.start, end=P90.end)
    assert list(frames.sales["amount_ars"]) == [1000.0]


async def test_inflow_presente_en_frame(db_session: AsyncSession, sample_tenant) -> None:
    tid = sample_tenant.tenant_id
    db_session.add(_sale(tid, _dt(TODAY), 2500, method="inflow"))
    await db_session.commit()

    frames = await load_facts_frames(db_session, tid, start=P90.start, end=P90.end)
    assert list(frames.sales["payment_method"]) == ["inflow"]  # FactsService decide


async def test_coverage_guard_falla_explicito(db_session: AsyncSession, sample_tenant) -> None:
    tid = str(sample_tenant.tenant_id)
    frames = await load_facts_frames(db_session, sample_tenant.tenant_id,
                                     start=P90.start, end=P90.end)
    provider = PreloadedFactsProvider(frames)
    fuera = Period.last_n_days(365, today=TODAY)  # empieza antes de la cobertura
    try:
        provider.sales(tid, fuera.start, fuera.end)
        raise AssertionError("debió fallar fuera de cobertura")
    except ValueError as exc:
        assert "cobertura" in str(exc)


async def test_tenant_guard_falla_explicito(db_session: AsyncSession, sample_tenant) -> None:
    # Un provider precargado es de UN tenant: pedir otro no devuelve data ajena
    # en silencio, falla explícito.
    frames = await load_facts_frames(db_session, sample_tenant.tenant_id,
                                     start=P90.start, end=P90.end)
    provider = PreloadedFactsProvider(frames)
    try:
        provider.sales(str(uuid.uuid4()), P90.start, P90.end)
        raise AssertionError("debió fallar por tenant distinto")
    except ValueError as exc:
        assert "tenant" in str(exc)


async def test_tenant_sin_datos_da_empty(db_session: AsyncSession, sample_tenant) -> None:
    svc = await build_facts_service(db_session, sample_tenant.tenant_id, P90)
    f = svc.ventas_periodo(str(sample_tenant.tenant_id), P90)
    assert f.provenance == Provenance.EMPTY
    assert f.value is None  # nunca $0 falso


async def test_aislamiento_de_tenant(db_session: AsyncSession, sample_tenant,
                                     second_tenant) -> None:
    db_session.add(_sale(second_tenant.tenant_id, _dt(TODAY), 50000))
    await db_session.commit()
    frames = await load_facts_frames(db_session, sample_tenant.tenant_id,
                                     start=P90.start, end=P90.end)
    assert frames.sales.empty  # la venta del otro tenant no se filtra acá


# ─────────────────────────────────────────────────────────────────────────────
# Reconciliación end-to-end: 90 días sintéticos (abril–junio 2026)
# ─────────────────────────────────────────────────────────────────────────────
# Seed determinístico (nada de random). Totales calculados a mano:
#   Ventas (90 días, i = 0..89 desde P90.start):
#     - cash 1000/día ............................ 90 × 1000  =  90.000
#     - credit_card 500/día ...................... 90 × 500   =  45.000
#     - fiado (account) 2000 si i%7==0 ........... 13 × 2000  =  26.000
#     → ventas_totales = 161.000, n = 193 (≥50 → confidence 1.0)
#     - inflow (cobro de fiado) 800 si i%7==3 .... 13 × 800   =  10.400 (NO es venta)
#   COGS line-items: cash de días 0..88 linkeadas a prod_a (unit_cost 800, qty 1)
#     → 89 × 800 = 71.200; la cash del día 89 va a prod_c (sin costo → NaN)
#   Gastos: OPEX RENT 200/día = 18.000; COGS INVENTORY 1500 si i%7==1 = 19.500
#     → gastos_totales = 37.500
#   Período anterior: 2 ventas cash de 10.000 (15/03 y 20/03) = 20.000
#   Trampas: 1 DEMO 5.000 (in-window), 1 voided 7.000 (in-window), 1 futura 99.999
#
#   Esperado:
#     ventas        = 161.000, variación = (161000-20000)/20000 = +705.0%
#     ticket        = 161000/193 = 834.2
#     margen_bruto  = (161000-71200)/161000 = 55.8%  [line_items, conf = cobertura
#                     por monto 89000/161000 = 0.55]
#     margen_neto   = (161000-37500)/161000 = 76.7%
#     caja_liquida  = 161000 − 26000 − 45000 + 10400 = 100.400
#     fiado         = 26.000
#     valor_stock   = 100×800 + 50×500 (+ 10×NaN skip) = 105.000
#     sobrestock    = solo prod_b (nunca vendido, stock 50×500 = 25.000)

VENTAS_90D = 161_000.0
INFLOWS_90D = 10_400.0
FIADO_90D = 26_000.0
DELAYED_90D = 45_000.0
CAJA_90D = 100_400.0


async def _seed_90_days(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    prod_a = Product(tenant_id=tenant_id, name="Coca 500", sale_price_ars=Decimal("1500"),
                     unit_cost_ars=Decimal("800"), stock_units=100)
    prod_b = Product(tenant_id=tenant_id, name="Lavandina", sale_price_ars=Decimal("700"),
                     unit_cost_ars=Decimal("500"), stock_units=50,
                     acquired_at=_dt(date(2026, 1, 10)))  # nunca vendido, alta vieja
    prod_c = Product(tenant_id=tenant_id, name="Bolsa camiseta", sale_price_ars=Decimal("100"),
                     unit_cost_ars=None, stock_units=10)
    session.add_all([prod_a, prod_b, prod_c])
    await session.flush()

    for i in range(90):
        d = _dt(P90.start + timedelta(days=i))
        linked = prod_a.id if i < 89 else prod_c.id
        session.add(_sale(tenant_id, d, 1000, product_id=linked, quantity=1))
        session.add(_sale(tenant_id, d, 500, method="credit_card"))
        if i % 7 == 0:
            session.add(_sale(tenant_id, d, 2000, method="account"))
        if i % 7 == 3:
            session.add(_sale(tenant_id, d, 800, method="inflow"))
        session.add(_expense(tenant_id, d, 200, "RENT", "OPEX"))
        if i % 7 == 1:
            session.add(_expense(tenant_id, d, 1500, "INVENTORY", "COGS"))

    # período anterior (para la variación %)
    session.add(_sale(tenant_id, _dt(date(2026, 3, 15)), 10000))
    session.add(_sale(tenant_id, _dt(date(2026, 3, 20)), 10000))

    # trampas: DEMO, voided y futura — ninguna debe contar
    session.add(_sale(tenant_id, _dt(TODAY), 5000, provenance="DEMO"))
    session.add(_sale(tenant_id, _dt(TODAY), 7000,
                      voided_at=datetime(2026, 6, 29, tzinfo=UTC),
                      void_reason="USER_CANCELLED"))
    session.add(_sale(tenant_id, _dt(date(2026, 7, 5)), 99999))
    await session.commit()


async def test_reconciliacion_90_dias(db_session: AsyncSession, sample_tenant) -> None:
    tid = sample_tenant.tenant_id
    await _seed_90_days(db_session, tid)
    svc = await build_facts_service(db_session, tid, P90)
    snap = svc.dashboard_snapshot(str(tid), P90)

    # Ventas: trampas fuera (DEMO/voided/futura), inflows fuera, confidence plena
    ventas = snap["ventas"]
    assert ventas.value == VENTAS_90D
    assert ventas.sample_size == 193
    assert ventas.confidence == 1.0
    assert ventas.comparison_value == 20000.0
    assert ventas.variation_pct == 705.0

    assert snap["ticket_promedio"].value == round(VENTAS_90D / 193, 2)  # 834.2

    # Margen bruto por line-items; neto por todos los gastos — y son distintos.
    # La confidence del bruto declara la cobertura de costo por monto: las cash
    # costeadas son 89.000 sobre 161.000 vendidos → 0.55.
    assert snap["margen_bruto"].value == 55.8
    assert snap["margen_bruto"].confidence == 0.55
    assert "line_items" in snap["margen_bruto"].source
    assert snap["margen_neto"].value == 76.7
    assert snap["margen_neto"].confidence == 1.0  # hay gastos cargados
    assert snap["margen_bruto"].value != snap["margen_neto"].value

    # Caja: sin fiado, sin tarjeta, con cobros de fiado
    assert snap["caja_liquida"].value == CAJA_90D
    assert snap["fiado_pendiente"].value == FIADO_90D

    # Invariante: caja − inflows + fiado + delayed == ventas
    assert (snap["caja_liquida"].value - INFLOWS_90D + FIADO_90D
            + DELAYED_90D) == snap["ventas"].value

    # Stock: fórmula única; prod_c sin costo no suma y la confidence lo declara (2/3)
    assert snap["valor_stock"].value == 105_000.0
    assert snap["valor_stock"].confidence == 0.67

    # Sobrestock: solo prod_b (nunca vendido, alta en enero → 171 días).
    # prod_a y prod_c vendieron dentro de la ventana → no se flaggean.
    frozen = svc.sobrestock(str(tid), today=TODAY)
    assert [f.value for f in frozen] == [25_000.0]

    # DEMO cuenta solo si include_demo=True
    con_demo = svc.ventas_periodo(str(tid), P90, include_demo=True)
    assert con_demo.value == VENTAS_90D + 5000.0

    # Dashboard y chat leen el MISMO hecho (cero divergencia posible)
    advice = svc.collect_for_advice(str(tid), "flujo_caja", P90)
    caja_chat = next(f for f in advice if f.metric == "caja_liquida")
    assert caja_chat.value == snap["caja_liquida"].value
