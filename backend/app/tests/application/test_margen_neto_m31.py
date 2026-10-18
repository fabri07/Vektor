"""M·3-1 — el margen NETO del mes cerrado, una sola definición.

Decisión del dueño (2026-10-16): neto = (ingreso − costo vendido − todo gasto
vivo que no sea mercadería) / ingreso, por mes calendario cerrado. Tarjeta, chat,
alertas y subscore leen el MISMO BusinessFact (el score, detrás de
``NET_MARGIN_ROLLOUT_TENANT_IDS``).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.facts_provider import margenes_del_tenant
from app.application.services.facts_service import Period
from app.config.net_margin_rollout import net_margin_enabled_for
from app.config.settings import get_settings
from app.domain.business_time import today_ar
from app.domain.margen import (
    MOTIVO_SIN_GASTOS,
    LineaDeMargen,
    medir_margen,
    medir_margen_neto,
)
from app.heuristics.health_engine import calculate_health_score, margin_net_ratio
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.state.business_state_service import (
    _deserialize_state,
    _serialize_state,
    compute_business_state,
)
from app.tests.state.test_business_state_service import FakeRedis

# ── Dominio ──────────────────────────────────────────────────────────────────


def _bruto(*lineas: tuple[str, str | None, bool], regimen: str | None = None) -> Any:
    return medir_margen(
        [LineaDeMargen(Decimal(i), None if c is None else Decimal(c), conf)
         for i, c, conf in lineas],
        regimen,
    )


def test_neto_resta_costo_vendido_y_gastos() -> None:
    bruto = _bruto(("10000", "6000", True))
    m = medir_margen_neto(bruto, Decimal("1500"), gastos_observados=True)
    assert m.estado == "confirmado"
    assert m.resultado == Decimal("2500")
    assert m.margen_pct == Decimal("25")


def test_neto_negativo_vale() -> None:
    m = medir_margen_neto(_bruto(("1000", "800", True)), Decimal("500"), gastos_observados=True)
    assert m.margen_pct == Decimal("-30")


def test_neto_hereda_estado_motivo_y_supuestos_del_bruto() -> None:
    bruto = _bruto(("10000", "6000", False))
    m = medir_margen_neto(bruto, Decimal("1000"), gastos_observados=True)
    assert m.estado == "estimado"
    assert m.motivo == bruto.motivo
    ri = medir_margen_neto(
        _bruto(("12100", "5000", True), regimen="responsable_inscripto"),
        Decimal("1000"), gastos_observados=True,
    )
    assert ri.estado == "estimado"
    assert ri.supuestos
    assert ri.ingreso_total == Decimal("10000")  # sin IVA supuesto
    assert ri.margen_pct == Decimal("40")


def test_neto_usa_el_costo_proyectado_del_bruto() -> None:
    # 60% del ingreso con costo (ratio 0,5): el costo vendido se proyecta a todo.
    bruto = _bruto(("6000", "3000", True), ("4000", None, False))
    m = medir_margen_neto(bruto, Decimal("1000"), gastos_observados=True)
    assert m.costo_vendido == Decimal("5000")
    assert m.margen_pct == Decimal("40")


def test_sin_bruto_no_hay_neto() -> None:
    bruto = _bruto(("1000", None, False))
    m = medir_margen_neto(bruto, Decimal("100"), gastos_observados=True)
    assert m.estado == "ausente"
    assert m.motivo == bruto.motivo
    assert m.margen_pct is None


def test_sin_gastos_cargados_no_hay_neto() -> None:
    m = medir_margen_neto(_bruto(("1000", "500", True)), Decimal(0), gastos_observados=False)
    assert m.estado == "ausente"
    assert m.motivo == MOTIVO_SIN_GASTOS
    assert m.margen_pct is None


# ── Períodos ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("hoy", "desde", "hasta"),
    [
        (date(2026, 10, 17), date(2026, 9, 1), date(2026, 9, 30)),
        (date(2026, 10, 1), date(2026, 9, 1), date(2026, 9, 30)),   # hoy = día 1
        (date(2026, 1, 15), date(2025, 12, 1), date(2025, 12, 31)),  # enero → diciembre
        (date(2026, 3, 31), date(2026, 2, 1), date(2026, 2, 28)),
        (date(2028, 3, 1), date(2028, 2, 1), date(2028, 2, 29)),    # bisiesto
    ],
)
def test_mes_cerrado(hoy: date, desde: date, hasta: date) -> None:
    p = Period.mes_cerrado(today=hoy)
    assert (p.start, p.end) == (desde, hasta)
    assert p.label == f"mes_{hasta:%Y_%m}"


def test_mes_en_curso() -> None:
    p = Period.mes_en_curso(today=date(2026, 10, 17))
    assert (p.start, p.end) == (date(2026, 10, 1), date(2026, 10, 17))
    uno = Period.mes_en_curso(today=date(2026, 10, 1))
    assert uno.start == uno.end == date(2026, 10, 1)


# ── Contra la base: chat = FactsService = score ──────────────────────────────


@pytest.fixture
def habilitar(monkeypatch: pytest.MonkeyPatch) -> Any:
    def _set(valores: list[str]) -> None:
        monkeypatch.setattr(get_settings(), "NET_MARGIN_ROLLOUT_TENANT_IDS", valores)

    return _set


def _en_mes_cerrado(dia: int) -> datetime:
    inicio = Period.mes_cerrado(today=today_ar()).start
    return datetime.combine(inicio + timedelta(days=dia), time(12))


def _venta(tid: Any, cuando: datetime, monto: str, costo: str) -> SaleEntry:
    return SaleEntry(
        tenant_id=tid, amount=Decimal(monto), quantity=1, payment_method="cash",
        transaction_date=cuando, provenance="REAL", cost_unit_ars=Decimal(costo),
        cost_ars=Decimal(costo), cost_confirmed=True, cost_origin="compra",
        cost_snapshot_version=1,
    )


def _gasto(tid: Any, cuando: datetime, monto: str, categoria: str,
           tipo: str = "OPEX", recurrente: bool = False) -> ExpenseEntry:
    return ExpenseEntry(
        tenant_id=tid, amount=Decimal(monto), category=categoria, expense_type=tipo,
        description=categoria, transaction_date=cuando, is_recurring=recurrente,
    )


def _historia_previa(tid: Any) -> SaleEntry:
    """Una venta 10 días antes del mes cerrado: la cuenta ya vendía (mes completo).

    Siempre fuera de los últimos 30 días (el mes cerrado empieza ≥ 28 días atrás).
    """
    inicio = Period.mes_cerrado(today=today_ar()).start
    return _venta(tid, datetime.combine(inicio - timedelta(days=10), time(12)), "1", "1")


async def _cargar_mes_cerrado(db: AsyncSession, tid: Any) -> None:
    db.add_all([
        _historia_previa(tid),
        _venta(tid, _en_mes_cerrado(2), "10000", "6000"),
        _venta(tid, _en_mes_cerrado(9), "10000", "6000"),
        _gasto(tid, _en_mes_cerrado(1), "2000", "RENT", recurrente=True),
        _gasto(tid, _en_mes_cerrado(5), "1000", "BANK_FEES"),          # no recurrente: cuenta
        _gasto(tid, _en_mes_cerrado(3), "50000", "INVENTORY", "COGS"),  # mercadería: no
    ])
    await db.commit()


async def test_el_neto_es_del_mes_cerrado(db_session: AsyncSession, sample_tenant: Any) -> None:
    tid = sample_tenant.tenant_id
    await _cargar_mes_cerrado(db_session, tid)
    margenes = await margenes_del_tenant(db_session, tid)
    assert margenes is not None
    neto = margenes.neto
    assert neto.period == Period.mes_cerrado(today=today_ar()).label
    # (20000 − 12000 − 2000 − 1000) / 20000
    assert neto.value == 25.0
    assert neto.detail is not None
    assert neto.detail["gastos_por_categoria"] == {"BANK_FEES": 1000.0, "RENT": 2000.0}
    assert neto.detail["estado"] == "confirmado"
    assert margenes.bruto_mes_en_curso is not None
    assert margenes.bruto_mes_en_curso.period == Period.mes_en_curso(today=today_ar()).label


async def test_score_habilitado_lee_el_mismo_neto(
    db_session: AsyncSession, sample_tenant: Any, habilitar: Any
) -> None:
    tid = sample_tenant.tenant_id
    habilitar([str(tid)])
    await _cargar_mes_cerrado(db_session, tid)
    margenes = await margenes_del_tenant(db_session, tid)
    assert margenes is not None
    state = await compute_business_state(tid, db_session, FakeRedis())  # type: ignore[arg-type]
    assert state.margin_net_source == "mes_cerrado"
    assert state.margin_net_pct == margenes.neto.value == 25.0
    assert state.margin_status == "confirmado"
    assert margin_net_ratio(state) == pytest.approx(0.25)
    assert calculate_health_score(state).margin_net_ratio == pytest.approx(0.25)
    # Sobrevive la caché.
    assert _deserialize_state(_serialize_state(state)).margin_net_pct == 25.0


async def test_score_sin_habilitar_sigue_con_la_formula_previa(
    db_session: AsyncSession, sample_tenant: Any, habilitar: Any
) -> None:
    tid = sample_tenant.tenant_id
    habilitar([])
    await _cargar_mes_cerrado(db_session, tid)
    state = await compute_business_state(tid, db_session, FakeRedis())  # type: ignore[arg-type]
    margenes = await margenes_del_tenant(db_session, tid)
    assert margenes is not None
    assert margenes.bruto.detail is not None
    assert state.margin_net_source == "fijos"
    assert state.margin_net_pct is None
    # El estado del subscore sigue siendo el del bruto de 30 días, no el del neto.
    assert state.margin_status == margenes.bruto.detail["estado"]


async def test_score_habilitado_sin_gastos_no_inventa(
    db_session: AsyncSession, sample_tenant: Any, habilitar: Any
) -> None:
    tid = sample_tenant.tenant_id
    habilitar([str(tid)])
    db_session.add_all([_historia_previa(tid), _venta(tid, _en_mes_cerrado(2), "10000", "6000")])
    await db_session.commit()
    state = await compute_business_state(tid, db_session, FakeRedis())  # type: ignore[arg-type]
    assert state.margin_status == "ausente"
    assert state.margin_reason == MOTIVO_SIN_GASTOS
    assert margin_net_ratio(state) is None
    assert calculate_health_score(state).score_margin is None


def test_la_compuerta_default_no_habilita_a_nadie() -> None:
    assert get_settings().NET_MARGIN_ROLLOUT_TENANT_IDS == []
    assert net_margin_enabled_for("ef97804b-0000-4000-8000-00000000abcd") is False


def test_historial_rotula_el_neto_del_mes_cerrado() -> None:
    from types import SimpleNamespace  # noqa: PLC0415

    from app.api.v1.health_scores import _margin_history_item  # noqa: PLC0415

    snap = SimpleNamespace(
        snapshot_date=datetime(2026, 10, 16, 12), score_margin=72,
        score_inputs_json={
            "margin_net_source": "mes_cerrado", "margin_net_pct": 25.0,
            "margin_status": "confirmado", "margin_sales_base": "20000",
            "cogs_sold_est": "12000", "monthly_fixed_expenses_est": "2000",
        },
    )
    item = _margin_history_item(snap)
    assert item.margin_basis == "neto_mes_cerrado"
    assert item.margin_pct == 25.0  # el del hecho, no (ventas − costo − fijos)


async def test_explicacion_del_score_dice_neto_del_mes_cerrado(
    db_session: AsyncSession, sample_tenant: Any, habilitar: Any
) -> None:
    from app.application.services.health_score_service import (  # noqa: PLC0415
        margin_explanation_for,
    )

    tid = sample_tenant.tenant_id
    habilitar([str(tid)])
    await _cargar_mes_cerrado(db_session, tid)
    state = await compute_business_state(tid, db_session, FakeRedis())  # type: ignore[arg-type]
    texto = margin_explanation_for(state, calculate_health_score(state))
    assert texto.startswith("Margen neto del último mes cerrado de 25.0%")
    assert "gastos operativos" in texto


# ── Revisión adversarial de M·3-1 ────────────────────────────────────────────


def test_mes_incompleto_no_da_neto() -> None:
    # Cuatro días de ventas contra el alquiler del mes entero daban −35% critical.
    bruto = _bruto(("40000", "24000", True))
    m = medir_margen_neto(
        bruto, Decimal("30000"), gastos_observados=True,
        datos_desde=date(2026, 9, 27), periodo_desde=date(2026, 9, 1),
    )
    assert m.estado == "ausente"
    assert m.motivo == "mes incompleto: los datos empiezan el 27/09/2026"
    completo = medir_margen_neto(
        bruto, Decimal("3000"), gastos_observados=True,
        datos_desde=date(2026, 9, 1), periodo_desde=date(2026, 9, 1),
    )
    assert completo.margen_pct == Decimal("32.5")


async def test_cuenta_que_empezo_en_el_mes_cerrado_no_tiene_neto(
    db_session: AsyncSession, sample_tenant: Any
) -> None:
    tid = sample_tenant.tenant_id
    db_session.add_all([
        _venta(tid, _en_mes_cerrado(26), "40000", "24000"),
        _gasto(tid, _en_mes_cerrado(26), "30000", "RENT"),
    ])
    await db_session.commit()
    margenes = await margenes_del_tenant(db_session, tid)
    assert margenes is not None
    assert margenes.neto.value is None
    assert margenes.neto.severity is None
    assert margenes.neto.detail is not None
    assert str(margenes.neto.detail["motivo"]).startswith("mes incompleto")


async def test_un_gasto_del_dia_1_no_completa_el_mes(
    db_session: AsyncSession, sample_tenant: Any
) -> None:
    # Revisión de la fase 2: el alquiler cargado el 1° hacía «completo» un mes en
    # que las ventas empiezan el 26, y el neto salía −35%.
    tid = sample_tenant.tenant_id
    db_session.add_all([
        _gasto(tid, _en_mes_cerrado(0), "30000", "RENT"),
        _venta(tid, _en_mes_cerrado(26), "40000", "24000"),
    ])
    await db_session.commit()
    margenes = await margenes_del_tenant(db_session, tid)
    assert margenes is not None
    assert margenes.neto.value is None
    assert margenes.neto.detail is not None
    assert str(margenes.neto.detail["motivo"]).startswith("mes incompleto")


async def test_un_cobro_de_fiado_viejo_no_completa_el_mes(
    db_session: AsyncSession, sample_tenant: Any
) -> None:
    tid = sample_tenant.tenant_id
    cobro = _venta(tid, _en_mes_cerrado(0) - timedelta(days=60), "100", "50")
    cobro.payment_method = "inflow"
    db_session.add_all([
        cobro,
        _venta(tid, _en_mes_cerrado(26), "40000", "24000"),
        _gasto(tid, _en_mes_cerrado(26), "30000", "RENT"),
    ])
    await db_session.commit()
    margenes = await margenes_del_tenant(db_session, tid)
    assert margenes is not None
    assert margenes.neto.value is None


async def test_una_venta_demo_vieja_no_completa_el_mes(
    db_session: AsyncSession, sample_tenant: Any
) -> None:
    tid = sample_tenant.tenant_id
    demo = _venta(tid, _en_mes_cerrado(0) - timedelta(days=60), "100", "50")
    demo.provenance = "DEMO"
    db_session.add_all([
        demo,
        _venta(tid, _en_mes_cerrado(26), "40000", "24000"),
        _gasto(tid, _en_mes_cerrado(26), "30000", "RENT"),
    ])
    await db_session.commit()
    margenes = await margenes_del_tenant(db_session, tid)
    assert margenes is not None
    assert margenes.neto.value is None


def test_compras_con_texto_libre_viejo_no_restan_como_gasto() -> None:
    from app.application.services.facts_service import es_categoria_de_mercaderia  # noqa: PLC0415

    for cat in ("INVENTORY", "mercaderia", "Mercadería", "compras", "compra proveedor",
                "reposicion"):
        assert es_categoria_de_mercaderia(cat), cat
    for otra in ("RENT", "TAXES", "OTHER", None):
        assert not es_categoria_de_mercaderia(otra), otra


async def test_si_el_neto_falla_el_bruto_sigue(
    db_session: AsyncSession, sample_tenant: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.application.services.facts_service import FactsService  # noqa: PLC0415

    def _explota(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("boom")

    tid = sample_tenant.tenant_id
    await _cargar_mes_cerrado(db_session, tid)
    monkeypatch.setattr(FactsService, "margen_neto", _explota)
    margenes = await margenes_del_tenant(db_session, tid)
    assert margenes is not None
    assert margenes.neto.value is None
    assert margenes.neto.detail == {"estado": "ausente", "motivo": "no se pudo medir ahora"}
    assert margenes.bruto.metric == "margen_bruto"


def test_la_alerta_de_margen_cae_al_bruto_si_el_neto_no_tiene_numero() -> None:
    import pandas as pd  # noqa: PLC0415

    from app.application.agents.shared.alert_explainer import resolve_alert_facts  # noqa: PLC0415
    from app.application.services.facts_service import FactsService  # noqa: PLC0415
    from app.tests.application.test_facts_reconciliation import (  # noqa: PLC0415
        _EXPENSES_COLS,
        _PRODUCTS_COLS,
        _SALES_COLS,
        KIOSCO_THRESHOLDS,
        FakeProvider,
    )

    hoy = date(2026, 10, 10)
    sales = pd.DataFrame(
        [{"sale_date": hoy, "amount_ars": 1000, "cost_ars": 900,
          "payment_method": "cash", "provenance": "REAL"}],
        columns=_SALES_COLS,
    )
    svc = FactsService(
        FakeProvider(sales, pd.DataFrame(columns=_EXPENSES_COLS),
                     pd.DataFrame(columns=_PRODUCTS_COLS)),
        KIOSCO_THRESHOLDS,
    )
    bloques = resolve_alert_facts(svc, "t", ["MARGIN_LOW"], Period.last_n_days(30, today=hoy))
    assert bloques[0]["fact"]["metric"] == "margen_bruto"
    assert bloques[0]["fact"]["value"] == 10.0


async def test_la_cache_ve_un_gasto_del_mes_cerrado(
    db_session: AsyncSession, sample_tenant: Any, habilitar: Any
) -> None:
    tid = sample_tenant.tenant_id
    habilitar([str(tid)])
    await _cargar_mes_cerrado(db_session, tid)
    redis = FakeRedis()
    antes = await compute_business_state(tid, db_session, redis)  # type: ignore[arg-type]
    assert antes.margin_net_pct == 25.0
    # Día 1 del mes cerrado: fuera de los 30 días que ya miraba el fingerprint
    # (salvo hoy = 1.º del mes, cuando el mes cerrado entero cae adentro).
    db_session.add(_gasto(tid, _en_mes_cerrado(0), "1000", "UTILITIES"))
    await db_session.commit()
    despues = await compute_business_state(tid, db_session, redis)  # type: ignore[arg-type]
    assert despues.margin_net_pct == 20.0  # (20000 − 12000 − 4000) / 20000


async def test_explicacion_sin_neto_no_dice_sin_costo(
    db_session: AsyncSession, sample_tenant: Any, habilitar: Any
) -> None:
    from app.application.services.health_score_service import (  # noqa: PLC0415
        margin_explanation_for,
    )

    tid = sample_tenant.tenant_id
    habilitar([str(tid)])
    db_session.add_all([_historia_previa(tid), _venta(tid, _en_mes_cerrado(2), "10000", "6000")])
    await db_session.commit()
    state = await compute_business_state(tid, db_session, FakeRedis())  # type: ignore[arg-type]
    texto = margin_explanation_for(state, calculate_health_score(state))
    assert texto == (
        "Sin margen neto del último mes cerrado (sin gastos cargados en el mes); "
        "no entra en el total."
    )
