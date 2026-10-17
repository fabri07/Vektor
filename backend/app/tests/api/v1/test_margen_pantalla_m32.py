"""M·3-2 — el margen en pantalla: todo de FactsService, sin fórmula en el front.

- ``GET /insights/margins``: neto del mes cerrado, bruto del mes en curso y del
  período, serie del bruto por tramo, serie del neto por mes cerrado y desglose
  del bruto por categoría; con la completitud < 50, vacío (no-invention).
- ``GET /sales``: costo y margen por venta sólo para dueño o admin.
- ``ProductResponse.margin_pct``: un costo 0 no es «margen 100%».
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.facts_provider import granularidad_para
from app.application.services.facts_service import Period, tramos
from app.domain.business_time import today_ar
from app.persistence.models.product import Product
from app.persistence.models.score import HealthScoreSnapshot
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.persistence.models.user import User
from app.schemas.product import ProductResponse

# ── Tramos y granularidad ────────────────────────────────────────────────────


def test_tramos_por_dia_semana_y_mes() -> None:
    p = Period(start=date(2026, 9, 28), end=date(2026, 10, 6), label="p")
    dias = tramos(p, "dia")
    assert len(dias) == 9 and dias[0].start == dias[0].end == date(2026, 9, 28)
    semanas = tramos(p, "semana")
    # lunes 28/9 → domingo 4/10, y el resto recortado al período
    assert [(s.start, s.end) for s in semanas] == [
        (date(2026, 9, 28), date(2026, 10, 4)), (date(2026, 10, 5), date(2026, 10, 6)),
    ]
    meses = tramos(p, "mes")
    assert [(m.start, m.end) for m in meses] == [
        (date(2026, 9, 28), date(2026, 9, 30)), (date(2026, 10, 1), date(2026, 10, 6)),
    ]
    completos = tramos(p, "mes", completos=True)
    assert [(m.start, m.end) for m in completos] == [
        (date(2026, 9, 1), date(2026, 9, 30)), (date(2026, 10, 1), date(2026, 10, 31)),
    ]


def test_tramos_por_mes_cruzan_el_año() -> None:
    p = Period(start=date(2025, 12, 15), end=date(2026, 2, 3), label="p")
    assert [m.label for m in tramos(p, "mes", completos=True)] == [
        "mes_2025_12", "mes_2026_01", "mes_2026_02",
    ]


@pytest.mark.parametrize(
    ("dias", "esperada"), [(31, "dia"), (32, "semana"), (120, "semana"), (121, "mes")]
)
def test_granularidad_segun_el_largo(dias: int, esperada: str) -> None:
    desde = date(2026, 1, 1)
    assert granularidad_para(desde, desde + timedelta(days=dias - 1)) == esperada


# ── Productos: costo 0 ───────────────────────────────────────────────────────


def _producto_response(costo: str | None, precio: str = "100") -> ProductResponse:
    p = Product(
        id=uuid.uuid4(), tenant_id=uuid.uuid4(), name="X", sale_price_ars=Decimal(precio),
        unit_cost_ars=None if costo is None else Decimal(costo), stock_units=0,
        is_active=True, created_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 1),
        sale_unit="unit", base_units_per_sale_unit=1, requires_completion=False,
    )
    return ProductResponse.model_validate(p)


def test_costo_cero_no_es_margen_100() -> None:
    r = _producto_response("0")
    assert r.margin_pct is None
    assert r.margin_motivo == "costo 0: falta cargar el costo"
    assert _producto_response(None).margin_motivo == "sin costo cargado"
    ok = _producto_response("60")
    assert ok.margin_pct == 40.0 and ok.margin_motivo is None


# ── Endpoint ─────────────────────────────────────────────────────────────────


def _cuando(d: date) -> datetime:
    return datetime.combine(d, time(12))


def _venta(tid: Any, d: date, monto: str, costo: str | None, *,
           producto: uuid.UUID | None = None, metodo: str = "cash",
           provenance: str = "REAL", anulada: bool = False) -> SaleEntry:
    snapshot = (
        {"cost_unit_ars": Decimal(costo), "cost_ars": Decimal(costo), "cost_confirmed": True,
         "cost_origin": "compra", "cost_snapshot_version": 1}
        if costo is not None else {}
    )
    extra: dict[str, Any] = {}
    if anulada:
        extra = {"voided_at": _cuando(d), "void_reason": "USER_CANCELLED"}
    return SaleEntry(
        tenant_id=tid, amount=Decimal(monto), quantity=1, payment_method=metodo,
        transaction_date=_cuando(d), provenance=provenance, product_id=producto,
        **snapshot, **extra,
    )


async def _escenario(db: AsyncSession, tid: Any) -> None:
    hoy = today_ar()
    mc = Period.mes_cerrado(today=hoy)
    bebidas = Product(tenant_id=tid, name="Coca", category="Bebidas",
                      sale_price_ars=Decimal("1000"), unit_cost_ars=Decimal("600"))
    db.add(bebidas)
    await db.flush()
    db.add_all([
        # historia previa: el mes cerrado está completo
        ExpenseEntry(tenant_id=tid, amount=Decimal("1"), category="OTHER",
                     expense_type="OPEX", description="x",
                     transaction_date=_cuando(mc.start - timedelta(days=10))),
        # mes cerrado: 20000 de ventas con costo 12000, 3000 de gastos operativos
        _venta(tid, mc.start + timedelta(days=2), "10000", "6000", producto=bebidas.id),
        _venta(tid, mc.start + timedelta(days=9), "10000", "6000", producto=bebidas.id),
        ExpenseEntry(tenant_id=tid, amount=Decimal("3000"), category="RENT",
                     expense_type="OPEX", description="alquiler",
                     transaction_date=_cuando(mc.start + timedelta(days=1))),
        # mes en curso (hoy): una con producto y costo, una sin producto
        _venta(tid, hoy, "1000", "500", producto=bebidas.id),
        _venta(tid, hoy, "1000", None),
        # trampas: anulada, demo y cobro de fiado no cuentan
        _venta(tid, hoy, "9999", "1", producto=bebidas.id, anulada=True),
        _venta(tid, hoy, "9999", "1", producto=bebidas.id, provenance="DEMO"),
        _venta(tid, hoy, "5000", None, metodo="inflow"),
    ])
    await db.commit()


async def test_margins_lee_los_hechos_de_facts(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Any,
) -> None:
    tid = sample_tenant.tenant_id
    await _escenario(db_session, tid)
    hoy = today_ar()
    r = await client.get("/api/v1/insights/margins", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sin_datos"] is False
    neto = body["neto_mes_cerrado"]
    assert neto["periodo"] == Period.mes_cerrado(today=hoy).label
    assert neto["value"] == 25.0  # (20000 − 12000 − 3000) / 20000
    assert neto["gastos_operativos_ars"] == 3000.0
    en_curso = body["bruto_mes_en_curso"]
    # 1000 con costo de 2000 vendidos: cobertura 50% (en el borde, presente)
    assert en_curso["cobertura_total_pct"] == 50.0
    assert en_curso["value"] == 50.0
    # Sin período: el mes en curso. Serie diaria y desglose con «Sin producto».
    assert body["granularidad"] == "dia"
    assert body["bruto_periodo"]["value"] == en_curso["value"]
    categorias = {c["categoria"]: c for c in body["por_categoria"]}
    assert categorias["Bebidas"]["value"] == 50.0
    assert categorias["Sin producto"]["value"] is None
    assert categorias["Sin producto"]["estado"] == "ausente"
    # El neto del mes en curso nunca entra a la serie del neto.
    assert all(p["periodo"] != f"mes_{hoy:%Y_%m}" for p in body["serie_neto"])


async def test_margins_con_periodo_largo_trae_el_neto_por_mes(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Any,
) -> None:
    await _escenario(db_session, sample_tenant.tenant_id)
    hoy = today_ar()
    desde = Period.mes_cerrado(today=hoy).start
    r = await client.get(
        "/api/v1/insights/margins",
        params={"from_date": desde.isoformat(), "to_date": hoy.isoformat()},
        headers=auth_headers,
    )
    body = r.json()
    assert [p["value"] for p in body["serie_neto"]] == [25.0]
    puntos = body["serie_bruto"]
    assert puntos and all("desde" in p and "hasta" in p for p in puntos)
    assert puntos[0]["desde"] == desde.isoformat()


async def test_margins_sin_completitud_es_vacio(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Any,
) -> None:
    tid = sample_tenant.tenant_id
    await _escenario(db_session, tid)
    ahora = datetime.now()
    db_session.add(HealthScoreSnapshot(
        tenant_id=tid, total_score=40, level="WARNING", dimensions=[],
        triggered_by="test", snapshot_date=ahora, created_at=ahora,
        data_completeness_score=Decimal("40"), confidence_level="LOW",
    ))
    await db_session.commit()
    body = (await client.get("/api/v1/insights/margins", headers=auth_headers)).json()
    assert body["sin_datos"] is True
    assert body["neto_mes_cerrado"] is None
    assert body["serie_bruto"] == []


async def test_margins_rechaza_periodo_invertido(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    r = await client.get(
        "/api/v1/insights/margins",
        params={"from_date": "2026-10-10", "to_date": "2026-10-01"},
        headers=auth_headers,
    )
    assert r.status_code == 422


async def test_margins_aisla_el_tenant(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
) -> None:
    from app.persistence.models.tenant import Tenant  # noqa: PLC0415

    otro = Tenant(tenant_id=uuid.uuid4(), legal_name="Otro", display_name="Otro",
                  status="active")
    db_session.add(otro)
    await db_session.flush()
    await _escenario(db_session, otro.tenant_id)
    body = (await client.get("/api/v1/insights/margins", headers=auth_headers)).json()
    assert body["bruto_mes_en_curso"]["ingreso_ars"] == 0.0
    assert body["por_categoria"] == []


# ── Ventas: costo y margen por rol ───────────────────────────────────────────


async def test_ventas_con_margen_para_el_dueno(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Any,
) -> None:
    await _escenario(db_session, sample_tenant.tenant_id)
    r = await client.get("/api/v1/sales", headers=auth_headers)
    assert r.status_code == 200
    filas = r.json()
    con_costo = [f for f in filas if f["costo_ars"] == 500.0]
    assert con_costo and con_costo[0]["margen_pct"] == 50.0
    assert con_costo[0]["margen_estado"] == "confirmado"
    sin_costo = [f for f in filas if f["amount"] == 1000.0 and f["costo_ars"] is None]
    assert sin_costo and sin_costo[0]["margen_estado"] == "ausente"
    cobro = [f for f in filas if f["payment_method"] == "inflow"]
    assert cobro and cobro[0]["margen_estado"] is None


async def test_ventas_sin_costo_para_un_visor(
    client: AsyncClient, viewer_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Any,
) -> None:
    await _escenario(db_session, sample_tenant.tenant_id)
    r = await client.get("/api/v1/sales", headers=viewer_headers)
    assert r.status_code == 200
    for f in r.json():
        assert f["costo_ars"] is None and f["margen_pct"] is None
        assert f["margen_estado"] is None
        assert "cost_ars" not in f


async def test_el_cajero_no_llega_a_ventas(
    client: AsyncClient, db_session: AsyncSession, sample_tenant: Any
) -> None:
    from app.utils.security import create_access_token  # noqa: PLC0415

    cajero = User(user_id=uuid.uuid4(), tenant_id=sample_tenant.tenant_id,
                  email="caja@kiosco.com", full_name="Caja", password_hash="x",
                  role_code="CASHIER", is_active=True)
    db_session.add(cajero)
    await db_session.commit()
    token = create_access_token({"sub": str(cajero.user_id),
                                 "tenant_id": str(sample_tenant.tenant_id),
                                 "role_code": "CASHIER"})
    headers = {"Authorization": f"Bearer {token}"}
    assert (await client.get("/api/v1/sales", headers=headers)).status_code == 403
    assert (await client.get("/api/v1/insights/margins", headers=headers)).status_code == 403


# ── Revisión adversarial de M·3-2 ────────────────────────────────────────────


@pytest.mark.parametrize("regimen", [None, "monotributo", "responsable_inscripto"])
def test_bruto_vectorizado_igual_que_una_linea_por_venta(regimen: str | None) -> None:
    import random  # noqa: PLC0415

    import pandas as pd  # noqa: PLC0415

    from app.application.services.facts_service import FactsService  # noqa: PLC0415
    from app.domain.margen import LineaDeMargen, medir_margen  # noqa: PLC0415
    from app.tests.application.test_facts_reconciliation import (  # noqa: PLC0415
        _EXPENSES_COLS,
        _PRODUCTS_COLS,
        _SALES_COLS,
        KIOSCO_THRESHOLDS,
        FakeProvider,
    )

    rnd = random.Random(7)
    hoy = date(2026, 10, 10)
    filas: list[dict[str, Any]] = []
    costos: list[float | None] = []
    for i in range(300):
        importe = round(rnd.uniform(10, 5000), 2)
        costo = None if i % 5 == 0 else round(importe * rnd.uniform(0.3, 1.1), 2)
        costos.append(costo)
        filas.append({"sale_date": hoy, "amount_ars": importe,
                      "cost_ars": float("nan") if costo is None else costo,
                      "cost_confirmed": i % 3 == 0, "payment_method": "cash",
                      "provenance": "REAL"})
    sales = pd.DataFrame(filas, columns=[*_SALES_COLS, "cost_confirmed"])
    svc = FactsService(FakeProvider(sales, pd.DataFrame(columns=_EXPENSES_COLS),
                                    pd.DataFrame(columns=_PRODUCTS_COLS)),
                       KIOSCO_THRESHOLDS, fiscal_condition_crudo=regimen)
    vec = svc.medir_margen("t", Period(start=hoy, end=hoy, label="d"))
    una = medir_margen(
        [LineaDeMargen(Decimal(str(f["amount_ars"])),
                       None if c is None else Decimal(str(c)),
                       bool(f["cost_confirmed"]) and c is not None)
         for f, c in zip(filas, costos, strict=True)],
        regimen,
    )
    assert vec.estado == una.estado
    assert vec.motivo == una.motivo
    for campo in ("margen_pct", "costo_vendido", "cobertura_total", "cobertura_confirmada"):
        a, b = getattr(vec, campo), getattr(una, campo)
        assert abs(a - b) < Decimal("1e-6"), campo


async def test_el_chat_no_ve_margen_100_con_costo_0(
    db_session: AsyncSession, sample_tenant: Any
) -> None:
    from app.persistence.repositories.product_repository import (  # noqa: PLC0415
        ProductRepository,
    )

    tid = sample_tenant.tenant_id
    db_session.add(Product(tenant_id=tid, name="Importado", sale_price_ars=Decimal("100"),
                           unit_cost_ars=Decimal("0")))
    await db_session.commit()
    filas = await ProductRepository(db_session).get_products_with_margin(tid)
    assert [f["margin_pct"] for f in filas if f["name"] == "Importado"] == [None]


async def test_una_venta_mixta_no_se_duplica(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Any,
) -> None:
    # Las líneas de una operación mixta van con payment_method="mixed" y el
    # importe por medio vive en pos_tenders: el margen sólo lee sales_entries.
    tid = sample_tenant.tenant_id
    hoy = today_ar()
    db_session.add(_venta(tid, hoy, "1000", "600", metodo="mixed"))
    await db_session.commit()
    body = (await client.get("/api/v1/insights/margins", headers=auth_headers)).json()
    assert body["bruto_mes_en_curso"]["ingreso_ars"] == 1000.0
    assert body["bruto_mes_en_curso"]["value"] == 40.0
