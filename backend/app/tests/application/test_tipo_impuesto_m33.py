"""M·3-3 — el tipo de impuesto de un gasto y lo que hace con el margen neto.

Decisión del dueño (2026-10-16): los impuestos pagados restan del neto (IIBB,
monotributo, tasas municipales…), salvo el IVA de un Responsable Inscripto: sus
ventas ya entran sin IVA, así que el IVA que paga no es gasto. Un impuesto sin
tipo resta igual y el neto lo avisa — nunca se adivina el tipo.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agents.shared.margen_texto import describir_margen_neto
from app.application.services.cash_service import save_expense
from app.application.services.facts_provider import _EXPENSES_COLS
from app.application.services.facts_service import FactsService, Period
from app.application.services.ingestion_import_service import bulk_import_unclassified
from app.domain.business_time import today_ar
from app.domain.expense_categories import (
    infer_tax_kind,
    normalize_expense_category,
    resolve_tax_kind,
)
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry
from app.persistence.models.unclassified_record import UnclassifiedRecord
from app.tests.application.test_facts_reconciliation import (
    _PRODUCTS_COLS,
    _SALES_COLS,
    KIOSCO_THRESHOLDS,
    FakeProvider,
)

# ── Dominio ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("texto", "tipo"),
    [
        ("IIBB", "iibb"),
        ("Ingresos Brutos marzo", "iibb"),
        ("Monotributo", "monotributo"),
        ("AFIP monotributo", "monotributo"),
        ("Pago de IVA", "iva"),
        ("ABL", "tasa_municipal"),
        ("Tasa municipal", "tasa_municipal"),
        ("Seguridad e higiene", "tasa_municipal"),
        # No dice el tipo: no se adivina.
        ("Impuestos", None),
        ("AFIP", None),
        ("ARCA", None),
        (None, None),
        ("", None),
    ],
)
def test_infer_tax_kind(texto: str | None, tipo: str | None) -> None:
    assert infer_tax_kind(texto) == tipo


def test_sin_fuzzy_un_typo_no_da_tipo() -> None:
    assert infer_tax_kind("ivva") is None
    assert infer_tax_kind("monotribto") is None


def test_el_primer_texto_que_dice_algo_gana() -> None:
    assert infer_tax_kind(None, "Impuestos", "IIBB marzo") == "iibb"
    assert infer_tax_kind("Monotributo", "IIBB") == "monotributo"


def test_el_tipo_solo_vive_en_taxes() -> None:
    assert resolve_tax_kind("RENT", explicit="iibb", textos=("IIBB",)) is None
    assert resolve_tax_kind("TAXES", explicit="iva", textos=("IIBB",)) == "iva"
    assert resolve_tax_kind("TAXES", textos=("IIBB",)) == "iibb"
    # Un explícito fuera del catálogo (salida de LLM) no entra: vale el texto.
    assert resolve_tax_kind("TAXES", explicit="ganancias", textos=("ABL",)) == "tasa_municipal"
    assert resolve_tax_kind("TAXES") is None


@pytest.mark.parametrize(
    "texto",
    # Revisión de M·3-3: alias de IVA atrapaban esto por fuzzy, y «seguridad e
    # higiene» es también un artículo de limpieza (mercadería).
    ["Compra con IVA", "Pago día", "pago de visa", "Saldo día", "Pago Eva",
     "Artículos de seguridad e higiene"],
)
def test_los_alias_de_impuestos_no_atrapan_otra_cosa(texto: str) -> None:
    assert normalize_expense_category(texto)[0] != "TAXES"


def test_tasa_municipal_es_impuestos() -> None:
    assert normalize_expense_category("Tasa municipal")[0] == "TAXES"
    assert infer_tax_kind("Tasas bancarias") is None


# ── El margen neto ───────────────────────────────────────────────────────────

_SEPT = Period(start=date(2026, 9, 1), end=date(2026, 9, 30), label="mes_2026_09")


def _svc(gastos: list[dict[str, Any]], regimen: str | None) -> FactsService:
    sales = pd.DataFrame(
        [{"sale_date": date(2026, 9, 10), "amount_ars": 12100, "cost_ars": 6000,
          "cost_confirmed": True, "payment_method": "cash", "provenance": "REAL"}],
        columns=[*_SALES_COLS, "cost_confirmed"],
    )
    expenses = pd.DataFrame(
        [{"expense_date": date(2026, 9, 5), "provenance": "REAL", "expense_type": "OPEX",
          **g} for g in gastos],
        columns=_EXPENSES_COLS,
    )
    return FactsService(
        FakeProvider(sales, expenses, pd.DataFrame(columns=_PRODUCTS_COLS)),
        KIOSCO_THRESHOLDS, fiscal_condition_crudo=regimen,
    )


_ALQUILER = {"amount_ars": 1000, "category": "RENT", "tax_kind": None}
_IIBB = {"amount_ars": 300, "category": "TAXES", "tax_kind": "iibb"}
_IVA = {"amount_ars": 500, "category": "TAXES", "tax_kind": "iva"}


def test_ri_el_iva_pagado_no_resta() -> None:
    con = _svc([_ALQUILER, _IIBB, _IVA], "responsable_inscripto").margen_neto("t", _SEPT)
    sin = _svc([_ALQUILER, _IIBB], "responsable_inscripto").margen_neto("t", _SEPT)
    assert con.value is not None
    assert con.value == sin.value
    assert con.detail is not None
    assert con.detail["gastos_operativos_ars"] == 1300.0
    assert con.detail["iva_excluido_ars"] == 500.0
    assert con.detail["gastos_por_categoria"] == {"RENT": 1000.0, "TAXES": 300.0}


def test_monotributo_el_iva_pagado_es_costo_y_la_cuota_resta() -> None:
    cuota = {"amount_ars": 200, "category": "TAXES", "tax_kind": "monotributo"}
    m = _svc([_ALQUILER, cuota, _IVA], "monotributo").margen_neto("t", _SEPT)
    assert m.detail is not None
    assert m.detail["gastos_operativos_ars"] == 1700.0
    assert m.detail["iva_excluido_ars"] == 0.0
    # (12100 − 6000 − 1700) / 12100
    assert m.value == 36.4


def test_un_tipo_iva_fuera_de_taxes_no_saca_nada() -> None:
    """Una reclasificación pudo dejar el tipo colgado: fuera de TAXES no vale."""
    colgado = {"amount_ars": 500, "category": "UTILITIES", "tax_kind": "iva"}
    m = _svc([_ALQUILER, colgado], "responsable_inscripto").margen_neto("t", _SEPT)
    assert m.detail is not None
    assert m.detail["gastos_operativos_ars"] == 1500.0
    assert m.detail["iva_excluido_ars"] == 0.0


def test_impuestos_sin_tipo_restan_y_se_avisan() -> None:
    sin_tipo = {"amount_ars": 400, "category": "TAXES", "tax_kind": None}
    f = _svc([_ALQUILER, sin_tipo, _IIBB], "responsable_inscripto").margen_neto("t", _SEPT)
    assert f.detail is not None
    assert f.detail["gastos_operativos_ars"] == 1700.0
    assert f.detail["impuestos_sin_tipo"] == 1
    assert f.detail["impuestos_sin_tipo_ars"] == 400.0
    texto = describir_margen_neto(f)
    assert "1 gastos de impuestos sin tipo ($400.0) se restaron igual" in texto


def test_el_chat_dice_que_el_iva_no_resta() -> None:
    f = _svc([_ALQUILER, _IVA], "responsable_inscripto").margen_neto("t", _SEPT)
    assert "IVA pagado $500.0 no resta" in describir_margen_neto(f)


def test_un_frame_sin_columna_de_tipo_sigue_andando() -> None:
    """Providers viejos (o fakes) sin ``tax_kind``: todo impuesto resta, sin tipo."""
    sales = pd.DataFrame(
        [{"sale_date": date(2026, 9, 10), "amount_ars": 1000, "cost_ars": 500,
          "payment_method": "cash", "provenance": "REAL"}],
        columns=_SALES_COLS,
    )
    expenses = pd.DataFrame(
        [{"expense_date": date(2026, 9, 5), "amount_ars": 100, "category": "TAXES",
          "expense_type": "OPEX", "provenance": "REAL"}],
        columns=["expense_date", "amount_ars", "category", "expense_type", "provenance"],
    )
    svc = FactsService(FakeProvider(sales, expenses, pd.DataFrame(columns=_PRODUCTS_COLS)),
                       KIOSCO_THRESHOLDS, fiscal_condition_crudo="responsable_inscripto")
    f = svc.margen_neto("t", _SEPT)
    assert f.detail is not None
    assert f.detail["gastos_operativos_ars"] == 100.0
    assert f.detail["impuestos_sin_tipo"] == 1


# ── Escritores ───────────────────────────────────────────────────────────────

_HOY = str(today_ar())


@pytest.fixture
def _sin_celery(mock_score_trigger: Any) -> None:
    return None


@pytest.mark.usefixtures("_sin_celery")
async def test_post_guarda_el_tipo_elegido(
    client: AsyncClient, auth_headers: dict[str, Any]
) -> None:
    resp = await client.post("/api/v1/expenses", headers=auth_headers, json={
        "amount": "300", "category": "TAXES", "tax_kind": "iibb", "expense_date": _HOY,
    })
    assert resp.status_code == 201
    assert resp.json()["tax_kind"] == "iibb"


@pytest.mark.usefixtures("_sin_celery")
async def test_post_sin_especificar_no_infiere_de_la_descripcion(
    client: AsyncClient, auth_headers: dict[str, Any]
) -> None:
    """«Plan de pagos AFIP – IVA y ganancias» no es IVA entero (revisión de M·3-3)."""
    resp = await client.post("/api/v1/expenses", headers=auth_headers, json={
        "amount": "300", "category": "TAXES",
        "description": "Plan de pagos AFIP - IVA y ganancias", "expense_date": _HOY,
    })
    assert resp.status_code == 201
    assert resp.json()["tax_kind"] is None


@pytest.mark.usefixtures("_sin_celery")
async def test_post_tipo_con_otra_categoria_es_422(
    client: AsyncClient, auth_headers: dict[str, Any]
) -> None:
    resp = await client.post("/api/v1/expenses", headers=auth_headers, json={
        "amount": "300", "category": "RENT", "tax_kind": "iibb", "expense_date": _HOY,
    })
    assert resp.status_code == 422
    resp = await client.post("/api/v1/expenses", headers=auth_headers, json={
        "amount": "300", "category": "TAXES", "tax_kind": "ganancias", "expense_date": _HOY,
    })
    assert resp.status_code == 422


@pytest.mark.usefixtures("_sin_celery")
async def test_patch_cambia_limpia_y_rechaza(
    client: AsyncClient, auth_headers: dict[str, Any]
) -> None:
    creado = await client.post("/api/v1/expenses", headers=auth_headers, json={
        "amount": "300", "category": "TAXES", "expense_date": _HOY,
    })
    gid = creado.json()["id"]
    assert creado.json()["tax_kind"] is None

    r = await client.patch(f"/api/v1/expenses/{gid}", headers=auth_headers,
                           json={"tax_kind": "iva"})
    assert r.json()["tax_kind"] == "iva"
    # Sin mandar el tipo, el tipo queda.
    r = await client.patch(f"/api/v1/expenses/{gid}", headers=auth_headers,
                           json={"amount": "310"})
    assert r.json()["tax_kind"] == "iva"
    # null explícito lo limpia.
    r = await client.patch(f"/api/v1/expenses/{gid}", headers=auth_headers,
                           json={"tax_kind": None})
    assert r.json()["tax_kind"] is None

    await client.patch(f"/api/v1/expenses/{gid}", headers=auth_headers,
                       json={"tax_kind": "iibb"})
    # Salir de TAXES lo limpia.
    r = await client.patch(f"/api/v1/expenses/{gid}", headers=auth_headers,
                           json={"category": "RENT"})
    assert r.json()["tax_kind"] is None
    # Tipo con otra categoría: 422.
    r = await client.patch(f"/api/v1/expenses/{gid}", headers=auth_headers,
                           json={"tax_kind": "iibb"})
    assert r.status_code == 422
    # Volver a TAXES sin tipo no revive el viejo.
    await client.patch(f"/api/v1/expenses/{gid}", headers=auth_headers,
                       json={"category": "TAXES", "tax_kind": "iva"})
    await client.patch(f"/api/v1/expenses/{gid}", headers=auth_headers,
                       json={"category": "RENT"})
    r = await client.patch(f"/api/v1/expenses/{gid}", headers=auth_headers,
                           json={"category": "TAXES"})
    assert r.json()["tax_kind"] is None


async def test_chat_registra_el_tipo(db_session: AsyncSession, sample_tenant: Tenant) -> None:
    g = await save_expense({"amount": 900, "category": "ingresos brutos"},
                           sample_tenant.tenant_id, db_session)
    assert (g.category, g.tax_kind) == ("TAXES", "iibb")
    g = await save_expense({"amount": 900, "category": "ABL"},
                           sample_tenant.tenant_id, db_session)
    assert (g.category, g.tax_kind) == ("TAXES", "tasa_municipal")
    # La descripción no fija el tipo.
    g = await save_expense({"amount": 900, "category": "impuestos",
                            "description": "pago de IVA"}, sample_tenant.tenant_id, db_session)
    assert (g.category, g.tax_kind) == ("TAXES", None)
    g = await save_expense({"amount": 900, "category": "alquiler", "tax_kind": "iva"},
                           sample_tenant.tenant_id, db_session)
    assert (g.category, g.tax_kind) == ("RENT", None)


async def test_import_por_alias_fija_el_tipo(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    for categoria in ("IIBB", "Impuestos"):
        db_session.add(UnclassifiedRecord(
            tenant_id=tid, source="reanalysis", context_label="Reanálisis: gastos",
            row_data={"fecha": "2026-05-02", "detalle": "Pago", "monto": "1000",
                      "categoria": categoria},
            suggested_entity="expense",
        ))
    await db_session.flush()

    counts = await bulk_import_unclassified(db_session, tid)
    assert counts["imported_expenses"] == 2
    gastos = (await db_session.execute(select(ExpenseEntry))).scalars().all()
    assert sorted((g.category, g.tax_kind or "") for g in gastos) == [
        ("TAXES", ""), ("TAXES", "iibb"),
    ]


async def test_tipar_iva_cambia_la_marca_del_cache(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revisión de M·3-3: tipar un impuesto como IVA cambia el neto de un RI sin
    cambiar la cantidad ni la suma de gastos; la marca del cache tiene que verlo."""
    from datetime import datetime, time  # noqa: PLC0415

    from app.config.settings import get_settings  # noqa: PLC0415
    from app.state.business_state_service import _marca_del_neto  # noqa: PLC0415

    tid = sample_tenant.tenant_id
    monkeypatch.setattr(get_settings(), "NET_MARGIN_ROLLOUT_TENANT_IDS", [str(tid)])
    mes = Period.mes_cerrado(today=today_ar())
    g = ExpenseEntry(tenant_id=tid, amount=500, category="TAXES", expense_type="OPEX",
                     description="impuesto", is_recurring=False,
                     transaction_date=datetime.combine(mes.start, time(12)))
    db_session.add(g)
    await db_session.flush()
    antes = await _marca_del_neto(db_session, tid)
    g.tax_kind = "iva"
    await db_session.flush()
    con_iva = await _marca_del_neto(db_session, tid)
    assert con_iva != antes
    g.tax_kind = None
    g.category = "RENT"
    await db_session.flush()
    assert await _marca_del_neto(db_session, tid) not in (antes, con_iva)


async def test_un_costeo_tardio_cambia_la_marca_del_cache(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revisión de la fase 2: el costeo retroactivo de una venta del mes cerrado
    cambia el costo vendido sin tocar su cantidad ni su importe."""
    from datetime import datetime, time  # noqa: PLC0415
    from decimal import Decimal  # noqa: PLC0415

    from app.config.settings import get_settings  # noqa: PLC0415
    from app.persistence.models.transaction import SaleEntry  # noqa: PLC0415
    from app.state.business_state_service import _marca_del_neto  # noqa: PLC0415

    tid = sample_tenant.tenant_id
    monkeypatch.setattr(get_settings(), "NET_MARGIN_ROLLOUT_TENANT_IDS", [str(tid)])
    mes = Period.mes_cerrado(today=today_ar())
    v = SaleEntry(tenant_id=tid, amount=Decimal("1000"), quantity=1, payment_method="cash",
                  transaction_date=datetime.combine(mes.start, time(12)))
    db_session.add(v)
    await db_session.flush()
    antes = await _marca_del_neto(db_session, tid)
    v.cost_ars, v.cost_unit_ars, v.cost_origin = Decimal("600"), Decimal("600"), "catalogo"
    await db_session.flush()
    estimada = await _marca_del_neto(db_session, tid)
    assert estimada != antes
    v.cost_confirmed = True
    await db_session.flush()
    assert await _marca_del_neto(db_session, tid) not in (antes, estimada)
