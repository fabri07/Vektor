"""M·P4-2 — el margen con costo promedio AL LADO del de reposición.

Es un dato, nunca un segundo margen: ``margen_bruto`` no cambia, la comparación
sale de la intersección (venta con costo de reposición y promedio ``medido``),
un RI sale ausente hasta M·P4b y el chat lo dice sólo con la cuenta habilitada.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Any

import pandas as pd
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agents.chat.agent import AgentChat
from app.application.agents.shared.margen_texto import describir_promedio_vs_reposicion
from app.application.services.facts_provider import margenes_del_tenant
from app.application.services.facts_service import FactsService, Provenance
from app.config.settings import get_settings
from app.domain.business_time import today_ar
from app.domain.margen import (
    MOTIVO_PROMEDIO_SIN_NETO_IVA,
    MOTIVO_SIN_INGRESO,
    LineaDeComparacion,
    comparar_promedio_y_reposicion,
)
from app.persistence.models.transaction import SaleEntry
from app.tests.application.test_facts_reconciliation import (
    _EXPENSES_COLS,
    _PRODUCTS_COLS,
    _SALES_COLS,
    KIOSCO_THRESHOLDS,
    P30,
    TODAY,
    FakeProvider,
)


def _l(importe: str, rep: str | None, prom: str | None,
       estado: str | None = "medido") -> LineaDeComparacion:
    return LineaDeComparacion(
        importe=Decimal(importe),
        costo_reposicion=None if rep is None else Decimal(rep),
        costo_promedio=None if prom is None else Decimal(prom),
        estado_promedio=estado,
    )


# ── Dominio ──────────────────────────────────────────────────────────────────


def test_intersección_mismo_denominador_y_brecha() -> None:
    c = comparar_promedio_y_reposicion(
        [
            _l("1000", "600", "500"),
            _l("1000", "700", "650"),
            _l("500", None, "100"),             # sin reposición: fuera
            _l("500", "100", "50", "incompleto"),  # promedio no medido: fuera
        ],
        None,
    )
    assert c.motivo_ausente is None
    assert c.ingreso_comun == Decimal("2000")
    assert c.cobertura_conjunta == Decimal("2000") / Decimal("3000")
    assert c.margen_reposicion_pct == Decimal("35")   # 1 − 1300/2000
    assert c.margen_promedio_pct == Decimal("42.5")   # 1 − 1150/2000
    assert c.brecha_pp == Decimal("7.5")
    assert c.brecha_ars == Decimal("150")


def test_brecha_negativa_con_precios_que_bajan() -> None:
    c = comparar_promedio_y_reposicion([_l("1000", "500", "600")], None)
    assert c.brecha_pp == Decimal("-10")
    assert c.brecha_ars == Decimal("-100")


@pytest.mark.parametrize(
    ("comun", "presente"), [("4999", False), ("5000", True), ("5001", True)]
)
def test_compuerta_de_cobertura_en_el_borde(comun: str, presente: bool) -> None:
    resto = str(10000 - int(comun))
    c = comparar_promedio_y_reposicion(
        [_l(comun, "100", "90"), _l(resto, None, None, None)], None
    )
    assert (c.motivo_ausente is None) is presente
    if not presente:
        assert c.motivo_ausente == "cobertura de costo 50,0%"  # 49,99 redondea
        assert c.margen_promedio_pct is None
        assert c.brecha_pp is None


@pytest.mark.parametrize("estado", ["incompleto", "sin_medicion", None])
def test_promedio_no_medido_queda_fuera(estado: str | None) -> None:
    c = comparar_promedio_y_reposicion([_l("1000", "500", "400", estado)], None)
    assert c.cobertura_conjunta == 0
    assert c.ausente


def test_ri_sale_ausente_aunque_haya_cobertura() -> None:
    c = comparar_promedio_y_reposicion(
        [_l("1000", "500", "400")], "responsable_inscripto"
    )
    assert c.motivo_ausente == MOTIVO_PROMEDIO_SIN_NETO_IVA
    assert c.cobertura_conjunta == 1
    assert c.margen_promedio_pct is None


def test_monotributo_y_legacy_registered_no_son_ri() -> None:
    for regimen in ("monotributo", "registered", None):
        assert comparar_promedio_y_reposicion([_l("1000", "500", "400")], regimen).ausente is False


def test_sin_ventas() -> None:
    assert comparar_promedio_y_reposicion([], None).motivo_ausente == MOTIVO_SIN_INGRESO


# ── FactsService ─────────────────────────────────────────────────────────────

_CON_PROMEDIO = [*_SALES_COLS, "cost_confirmed", "cost_avg_ars", "cost_avg_estado"]


def _svc(rows: list[dict[str, Any]], cols: list[str], regimen: str | None = None) -> FactsService:
    sales = pd.DataFrame(rows, columns=cols)
    return FactsService(
        FakeProvider(sales, pd.DataFrame(columns=_EXPENSES_COLS),
                     pd.DataFrame(columns=_PRODUCTS_COLS)),
        KIOSCO_THRESHOLDS, fiscal_condition_crudo=regimen,
    )


_FILAS = [
    {"sale_date": TODAY, "amount_ars": 1000, "cost_ars": 600, "cost_confirmed": True,
     "cost_avg_ars": 500, "cost_avg_estado": "medido",
     "payment_method": "cash", "provenance": "REAL"},
    {"sale_date": TODAY, "amount_ars": 1000, "cost_ars": 700, "cost_confirmed": True,
     "cost_avg_ars": float("nan"), "cost_avg_estado": None,
     "payment_method": "cash", "provenance": "REAL"},
    # Un cobro de fiado no es venta, ni para el margen ni para la comparación.
    {"sale_date": TODAY, "amount_ars": 9000, "cost_ars": float("nan"),
     "cost_confirmed": False, "cost_avg_ars": float("nan"), "cost_avg_estado": None,
     "payment_method": "inflow", "provenance": "REAL"},
]


def test_el_hecho_lleva_la_comparación_en_detail() -> None:
    f = _svc(_FILAS, _CON_PROMEDIO).margen_promedio_vs_reposicion("t", P30)
    assert f.metric == "margen_promedio_vs_reposicion"
    assert f.value == 50.0
    assert f.provenance == Provenance.REAL
    assert f.detail == {
        "motivo": None,
        "cobertura_conjunta_pct": 50.0,
        "ingreso_comun_ars": 1000.0,
        "margen_reposicion_comun_pct": 40.0,
        "margen_promedio_comun_pct": 50.0,
        "brecha_pp": 10.0,
        "brecha_ars": 100.0,
    }


def test_ausente_es_empty_sin_numero() -> None:
    f = _svc(_FILAS, _CON_PROMEDIO, "responsable_inscripto").margen_promedio_vs_reposicion(
        "t", P30
    )
    assert f.value is None
    assert f.provenance == Provenance.EMPTY
    assert f.detail is not None
    assert f.detail["motivo"] == MOTIVO_PROMEDIO_SIN_NETO_IVA
    assert f.detail["brecha_pp"] is None


def test_margen_bruto_idéntico_con_y_sin_columnas_de_promedio() -> None:
    sin = [{k: v for k, v in r.items() if not k.startswith("cost_avg")} for r in _FILAS]
    con_promedio = _svc(_FILAS, _CON_PROMEDIO).margen_bruto("t", P30)
    sin_promedio = _svc(sin, [*_SALES_COLS, "cost_confirmed"]).margen_bruto("t", P30)
    assert con_promedio == sin_promedio
    assert con_promedio.value == 35.0  # 1 − 1300/2000: la reposición, no el promedio


def test_provider_sin_columnas_de_promedio_da_ausente() -> None:
    sin = [{k: v for k, v in r.items() if not k.startswith("cost_avg")} for r in _FILAS]
    f = _svc(sin, [*_SALES_COLS, "cost_confirmed"]).margen_promedio_vs_reposicion("t", P30)
    assert f.value is None
    assert f.detail is not None
    assert f.detail["cobertura_conjunta_pct"] == 0.0


# ── Provider contra la base + chat ───────────────────────────────────────────


@pytest.fixture
def habilitar(monkeypatch: pytest.MonkeyPatch) -> Any:
    def _set(valores: list[str]) -> None:
        monkeypatch.setattr(get_settings(), "AVG_COST_DISPLAY_ROLLOUT_TENANT_IDS", valores)

    return _set


def _venta(tid: Any, monto: str, costo: str, promedio: str | None,
           estado: str | None) -> SaleEntry:
    return SaleEntry(
        tenant_id=tid, amount=Decimal(monto), quantity=1, payment_method="cash",
        transaction_date=datetime.combine(today_ar() - timedelta(days=1), time(12)),
        provenance="REAL", cost_unit_ars=Decimal(costo), cost_ars=Decimal(costo),
        cost_confirmed=True, cost_origin="compra", cost_snapshot_version=1,
        cost_avg_ars=None if promedio is None else Decimal(promedio),
        cost_avg_estado=estado,
    )


async def _cargar(db: AsyncSession, tid: Any) -> None:
    db.add_all([
        _venta(tid, "1000", "600", "500", "medido"),
        _venta(tid, "1000", "700", "300", "incompleto"),  # fuera de la intersección
    ])
    await db.commit()


async def test_provider_lee_el_promedio_de_la_base(
    db_session: AsyncSession, sample_tenant: Any, habilitar: Any
) -> None:
    tid = sample_tenant.tenant_id
    habilitar([str(tid)])
    await _cargar(db_session, tid)
    margenes = await margenes_del_tenant(db_session, tid)
    assert margenes is not None
    f = margenes.promedio_vs_reposicion
    assert f is not None
    assert f.value == 50.0
    assert f.detail is not None
    assert f.detail["margen_reposicion_comun_pct"] == 40.0
    assert f.detail["cobertura_conjunta_pct"] == 50.0
    assert margenes.bruto.value == 35.0  # el principal no cambia


async def test_sin_habilitar_no_hay_dato_ni_texto(
    db_session: AsyncSession, sample_tenant: Any, habilitar: Any
) -> None:
    tid = sample_tenant.tenant_id
    habilitar([])
    await _cargar(db_session, tid)
    margenes = await margenes_del_tenant(db_session, tid)
    assert margenes is not None
    assert margenes.promedio_vs_reposicion is None
    contexto = await AgentChat()._load_financial_context(tid, db_session)
    assert "costo promedio" not in contexto
    assert "Margen bruto (ventas − costo de lo vendido): 35.0%" in contexto


async def test_chat_lo_dice_como_dato_aparte(
    db_session: AsyncSession, sample_tenant: Any, habilitar: Any
) -> None:
    tid = sample_tenant.tenant_id
    habilitar([str(tid)])
    await _cargar(db_session, tid)
    contexto = await AgentChat()._load_financial_context(tid, db_session)
    assert "Margen bruto (ventas − costo de lo vendido): 35.0%" in contexto
    assert (
        "Dato aparte del margen bruto: efecto inflación sobre el stock (ganancia por "
        "tenencia): 10.0 puntos, $100.00, en las ventas con costo de reposición y costo "
        "promedio (cobertura 50.0%); con costo promedio esas ventas darían 50.0%. "
        "NO es otro margen ni se suma al margen bruto"
    ) in contexto
    # El margen de reposición de la intersección (otro universo) no se imprime.
    assert "40.0%" not in contexto


async def test_si_el_dato_falla_bruto_y_neto_siguen(
    db_session: AsyncSession, sample_tenant: Any, habilitar: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.application.services import facts_provider  # noqa: PLC0415

    def _explota(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("boom")

    tid = sample_tenant.tenant_id
    habilitar([str(tid)])
    await _cargar(db_session, tid)
    monkeypatch.setattr(FactsService, "margen_promedio_vs_reposicion", _explota)
    lectura = await facts_provider.leer_margenes(db_session, tid)
    assert lectura.margenes is not None
    assert lectura.margenes.bruto.value == 35.0
    assert lectura.margenes.promedio_vs_reposicion is None


def test_con_precios_que_bajan_es_pérdida_por_tenencia() -> None:
    filas = [{**_FILAS[0], "cost_avg_ars": 700}]
    f = _svc(filas, _CON_PROMEDIO).margen_promedio_vs_reposicion("t", P30)
    texto = describir_promedio_vs_reposicion(f)
    assert texto is not None
    assert "(pérdida por tenencia): 10.0 puntos, $100.00" in texto
    assert "-" not in texto.split("NO es")[0]


def test_vectorizado_igual_al_dominio_línea_por_línea() -> None:
    filas: list[dict[str, Any]] = [
        {**_FILAS[0], "amount_ars": 1234.56, "cost_ars": 700.11, "cost_avg_ars": 650.07},
        {**_FILAS[0], "amount_ars": 999.99, "cost_ars": 333.33, "cost_avg_ars": 300.01},
        {**_FILAS[1]},
        {**_FILAS[0], "amount_ars": 50, "cost_ars": 10, "cost_avg_ars": 5,
         "cost_avg_estado": "sin_medicion"},
    ]
    vec = _svc(filas, _CON_PROMEDIO).comparar_promedio_y_reposicion("t", P30)
    def _d(x: Any) -> Decimal | None:
        return None if pd.isna(x) else Decimal(str(x))
    uno_a_uno = comparar_promedio_y_reposicion(
        [LineaDeComparacion(Decimal(str(r["amount_ars"])), _d(r["cost_ars"]),
                            _d(r["cost_avg_ars"]), r["cost_avg_estado"]) for r in filas],
        None,
    )
    for campo in ("margen_reposicion_pct", "margen_promedio_pct", "brecha_pp",
                  "brecha_ars", "cobertura_conjunta"):
        a, b = getattr(vec, campo), getattr(uno_a_uno, campo)
        assert abs(a - b) < Decimal("1e-9"), campo


def test_texto_ausente_no_dice_nada() -> None:
    f = _svc(_FILAS, _CON_PROMEDIO, "responsable_inscripto").margen_promedio_vs_reposicion(
        "t", P30
    )
    assert describir_promedio_vs_reposicion(f) is None
    assert describir_promedio_vs_reposicion(None) is None
