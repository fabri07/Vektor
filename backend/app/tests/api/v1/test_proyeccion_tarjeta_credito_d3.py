"""D-3 (resto, R-10) — la tarjeta de crédito entra a caja N días después de la venta.

Revisión profunda: la proyección de caja (`GET /forecast/cash`) sumaba la venta
con tarjeta de crédito el día que se hacía, aunque esa plata se acredita
después. Decisión del dueño (2026-10-28): entra N días corridos después de su
fecha, N configurable por tenant en `/settings/health-config` (0–60, por
defecto 14 ≈ 10 días hábiles, el plazo legal de liquidación).

Los escenarios A–D son los de la revisión del PR #217: con el crédito afuera
para siempre, el chat daba una alerta de liquidez falsa (A), un día con sólo
crédito bajaba el tier (B), un negocio que vende sólo con crédito veía «Sin
datos» (C) y unos días con sólo crédito marcaban la proyección vieja (D).
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agents.health.agent import AgentHealth
from app.application.agents.shared.schemas import AgentRequest
from app.application.services import forecast_service
from app.application.services.cash_service import normalize_payment_method
from app.application.services.forecast_service import (
    _cache_key,
    _compute_forecast,
    credito_por_dia,
    ingresos_de_caja_por_dia,
)
from app.domain.business_time import today_ar
from app.persistence.models.business import BusinessProfile
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.tests.integration.test_pago_mixto_lectores import _operacion

HOY = today_ar()
URL = "/api/v1/forecast/cash"
CONFIG = "/api/v1/settings/health-config"


def _venta(tid: Any, importe: str, metodo: str, dia: date) -> SaleEntry:
    return SaleEntry(
        tenant_id=tid,
        amount=Decimal(importe),
        quantity=1,
        transaction_date=datetime.combine(dia, time(12, 0)),
        payment_method=metodo,
        provenance="REAL",
    )


def _gasto(tid: Any, importe: str, dia: date) -> ExpenseEntry:
    return ExpenseEntry(
        tenant_id=tid,
        amount=Decimal(importe),
        category="OTHER",
        transaction_date=datetime.combine(dia, time(12, 0)),
        description="Gasto diario",
        is_recurring=False,
        payment_method="cash",
    )


async def _sembrar_efectivo_y_un_credito(db: AsyncSession, tid: Any, fin: date = HOY) -> None:
    """20 días con $1.000 en efectivo y $600 de gasto; $7.000 con crédito sólo
    3 días antes de ``fin``."""
    for i in range(20):
        dia = fin - timedelta(days=i)
        db.add(_venta(tid, "1000", "cash", dia))
        db.add(_gasto(tid, "600", dia))
    db.add(_venta(tid, "7000", "credit_card", fin - timedelta(days=3)))
    await db.commit()


def _por_fecha(data: dict[str, Any]) -> dict[str, float]:
    return {p["date"]: p["income"] for p in data["points"]}


# ── N por defecto, cambiado y 0 ───────────────────────────────────────────────


async def test_con_n_por_defecto_el_credito_entra_14_dias_despues(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    await _sembrar_efectivo_y_un_credito(db_session, tid)

    resp = await client.get(URL, params={"refresh": "true"}, headers=auth_headers)

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["tier"] == 1
    assert data["credit_card_settlement_days"] == 14
    ingresos = _por_fecha(data)
    acredita = (HOY - timedelta(days=3) + timedelta(days=14)).isoformat()
    assert ingresos[acredita] == 8000
    # El resto de los días: sólo el efectivo; el crédito no se reparte ni duplica.
    assert {v for d, v in ingresos.items() if d != acredita} == {1000}
    # Vendido hace 3 días con N = 14: todavía no se acreditó.
    assert data["credit_card_pending_ars"] == 7000.0
    # Las ventas con crédito siguen siendo ventas: la proyección no las toca.
    vivas = (
        await db_session.execute(
            select(func.count()).where(
                SaleEntry.tenant_id == tid,
                SaleEntry.payment_method == "credit_card",
                SaleEntry.voided_at.is_(None),
            )
        )
    ).scalar_one()
    assert vivas == 1


def _con_setex(fake_redis: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """El FakeRedis compartido no tiene `setex`: sin esto la caché nunca se escribe."""

    async def setex(key: str, ttl: int, value: str) -> None:
        await fake_redis.set(key, value, ex=ttl)

    monkeypatch.setattr(fake_redis, "setex", setex, raising=False)


async def test_cambiar_n_por_patch_mueve_la_acreditacion_y_no_sirve_la_cache_vieja(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    fake_redis: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = sample_tenant.tenant_id
    _con_setex(fake_redis, monkeypatch)
    await _sembrar_efectivo_y_un_credito(db_session, tid)
    antes = await client.get(URL, headers=auth_headers)  # cachea con N = 14
    assert antes.json()["credit_card_settlement_days"] == 14
    assert await fake_redis.get(_cache_key(tid, 14)) is not None

    patch = await client.patch(
        CONFIG, json={"credit_card_settlement_days": 5}, headers=auth_headers
    )
    assert patch.status_code == 200, patch.text
    assert patch.json()["credit_card_settlement_days"] == 5

    # Sin ?refresh: la clave de la caché lleva N, así que no sirve el blob de N = 14.
    resp = await client.get(URL, headers=auth_headers)

    data = resp.json()
    assert data["credit_card_settlement_days"] == 5
    ingresos = _por_fecha(data)
    # Días 1–5: crédito real vendido hace 4…0 días (los $7.000, el día 2).
    assert ingresos[(HOY + timedelta(days=2)).isoformat()] == 8000
    assert ingresos[(HOY + timedelta(days=1)).isoformat()] == 1000
    # Días 6–14: crédito de días de venta futuros, el promedio ($7.000 / 20).
    assert ingresos[(HOY + timedelta(days=11)).isoformat()] == 1350
    assert data["credit_card_pending_ars"] == 7000.0


async def test_con_n_cero_el_credito_cuenta_el_dia_de_la_venta(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """N = 0: lo vendido con crédito es caja ese día, como el efectivo. Los días
    futuros proyectan el promedio de crédito ($7.000 / 20 días) sobre el líquido."""
    await _sembrar_efectivo_y_un_credito(db_session, sample_tenant.tenant_id)
    await client.patch(CONFIG, json={"credit_card_settlement_days": 0}, headers=auth_headers)

    resp = await client.get(URL, params={"refresh": "true"}, headers=auth_headers)

    data = resp.json()
    assert data["credit_card_settlement_days"] == 0
    assert {p["income"] for p in data["points"]} == {1350}
    assert data["credit_card_pending_ars"] == 0.0


async def test_lo_pendiente_es_solo_lo_que_todavia_no_se_acredito(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """M-4: lo vendido hace 20 días con N = 14 ya se acreditó; no es pendiente."""
    tid = sample_tenant.tenant_id
    db_session.add(_venta(tid, "5000", "credit_card", HOY - timedelta(days=20)))
    db_session.add(_venta(tid, "300", "credit_card", HOY - timedelta(days=14)))
    db_session.add(_venta(tid, "700", "credit_card", HOY - timedelta(days=13)))
    db_session.add(_venta(tid, "200", "credit_card", HOY))
    await db_session.commit()

    r = await _compute_forecast(tid, db_session, 14)

    assert r.credito_sin_acreditar_ars == 900.0


# ── Escenarios de la revisión ─────────────────────────────────────────────────


async def _escenario_a(db: AsyncSession, tid: Any) -> None:
    """35 días: $1.000 en efectivo, $500 con crédito y $1.200 de gasto."""
    for i in range(35):
        dia = HOY - timedelta(days=i)
        db.add(_venta(tid, "1000", "cash", dia))
        db.add(_venta(tid, "500", "credit_card", dia))
        db.add(_gasto(tid, "1200", dia))
    await db.commit()


async def test_a_el_credito_acreditado_saca_la_proyeccion_de_negativo(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    await _escenario_a(db_session, sample_tenant.tenant_id)

    r = await _compute_forecast(sample_tenant.tenant_id, db_session)

    assert (r.tier, r.confidence, r.horizon_days) == (2, "MEDIUM", 30)
    # Días 1–14: crédito real vendido hace 13…0 días; 15–30: el esperado.
    assert {p.income for p in r.points} == {1500}
    assert {p.net for p in r.points} == {300}


async def test_a_el_chat_no_da_alerta_de_liquidez_falsa(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sample_business_profile: BusinessProfile,
) -> None:
    tid = sample_tenant.tenant_id
    await _escenario_a(db_session, tid)
    agente = AgentHealth(db=db_session)
    request = AgentRequest(user_id="u", business_id=str(tid), message="¿me quedo sin caja?")

    resp = await agente._handle_cashflow_scenario(request, "alertar_falta_liquidez")

    assert resp.result is not None
    assert resp.result["summary"] == "liquidez_ok", resp.message
    assert resp.result["structured_data"].get("risk_code") != "CASH_LOW"


async def test_el_chat_dice_a_cuantos_dias_cuenta_el_credito(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    await _escenario_a(db_session, tid)
    request = AgentRequest(user_id="u", business_id=str(tid), message="proyectá mi caja")

    resp = await AgentHealth(db=db_session)._handle_cashflow_scenario(request, "proyectar_caja")

    assert "tarjeta de crédito las cuento a 14 días" in (resp.message or "")
    assert resp.result is not None
    assert resp.result["structured_data"]["credit_card_settlement_days"] == 14


async def test_b_un_dia_con_solo_credito_cuenta_como_dia_con_datos(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    for i in range(29):
        dia = HOY - timedelta(days=i)
        db_session.add(_venta(tid, "1000", "cash", dia))
        db_session.add(_gasto(tid, "100", dia))
    db_session.add(_venta(tid, "1000", "credit_card", HOY - timedelta(days=29)))
    await db_session.commit()

    r = await _compute_forecast(tid, db_session)

    assert (r.data_days, r.tier, r.confidence, r.horizon_days) == (30, 2, "MEDIUM", 30)


async def test_c_un_negocio_que_vende_solo_con_credito_proyecta(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    for i in range(40):
        db_session.add(_venta(tid, "1000", "credit_card", HOY - timedelta(days=i)))
    await db_session.commit()

    resp = await client.get(URL, params={"refresh": "true"}, headers=auth_headers)

    data = resp.json()
    assert (data["tier"], data["confidence"], data["data_days"]) == (2, "MEDIUM", 40)
    assert data["message"] is None
    assert {p["income"] for p in data["points"]} == {1000}
    assert data["credit_card_pending_ars"] == 14000.0


async def test_d_los_ultimos_dias_con_solo_credito_no_marcan_la_proyeccion_vieja(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    for i in range(40):
        dia = HOY - timedelta(days=i)
        db_session.add(_venta(tid, "1000", "credit_card" if i < 10 else "cash", dia))
        if i >= 10:
            db_session.add(_gasto(tid, "100", dia))
    await db_session.commit()

    r = await _compute_forecast(tid, db_session)

    assert (r.tier, r.confidence, r.message) == (2, "MEDIUM", None)


async def test_el_dia_de_semana_promedia_sobre_los_dias_con_cualquier_venta(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """m-1: los sábados venden sólo con crédito. Su ingreso líquido esperado es
    0, no el promedio general; con N = 0 el sábado proyecta sólo su crédito."""
    tid = sample_tenant.tenant_id
    for i in range(35):
        dia = HOY - timedelta(days=i)
        if dia.weekday() == 5:
            db_session.add(_venta(tid, "700", "credit_card", dia))
        else:
            db_session.add(_venta(tid, "1000", "cash", dia))
    await db_session.commit()

    r = await _compute_forecast(tid, db_session, 0)

    for p in r.points:
        dia = date.fromisoformat(p.date)
        assert p.income == (700 if dia.weekday() == 5 else 1000), p


async def test_la_proyeccion_corta_en_el_dia_de_negocio(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """m-3: el «hoy» de la proyección es `today_ar()`, no el reloj del servidor."""
    hoy_negocio = HOY - timedelta(days=30)
    monkeypatch.setattr(forecast_service, "today_ar", lambda: hoy_negocio)
    await _sembrar_efectivo_y_un_credito(db_session, sample_tenant.tenant_id, fin=hoy_negocio)

    r = await _compute_forecast(sample_tenant.tenant_id, db_session)

    assert r.points[0].date == (hoy_negocio + timedelta(days=1)).isoformat()
    assert r.message is None


async def _sembrar_tier1(db: AsyncSession, tid: Any) -> None:
    for i in range(20):
        dia = HOY - timedelta(days=i)
        db.add(_venta(tid, "1000", "cash", dia))
        db.add(_venta(tid, str(300 + 10 * i), "credit_card", dia))


async def _sembrar_tier2(db: AsyncSession, tid: Any) -> None:
    for i in range(45):
        dia = HOY - timedelta(days=i)
        db.add(_venta(tid, "1000", "cash", dia))
        if dia.weekday() in (4, 5):
            db.add(_venta(tid, "3000", "credit_card", dia))


async def _sembrar_tier3(db: AsyncSession, tid: Any) -> None:
    """El escenario de la revalidación: 120 días, efectivo creciente y crédito
    sólo viernes y sábados, también creciente."""
    for i in range(120):
        dia = HOY - timedelta(days=i)
        db.add(_venta(tid, str(1000 + (120 - i) * 5), "cash", dia))
        if dia.weekday() in (4, 5):
            db.add(_venta(tid, str(800 + (120 - i) * 20), "credit_card", dia))
        db.add(_gasto(tid, "900", dia))


@pytest.mark.parametrize(
    ("sembrar", "tier"), [(_sembrar_tier1, 1), (_sembrar_tier2, 2), (_sembrar_tier3, 3)]
)
async def test_con_n_cero_es_lo_mismo_que_contar_el_credito_como_caja(
    db_session: AsyncSession, sample_tenant: Tenant, sembrar: Any, tier: int
) -> None:
    """N-1: con N = 0 cada tier da exactamente lo mismo que el ingreso total
    tratado como caja. En tier 3 el modelo del crédito ralo inflaba un 26 %."""
    tid = sample_tenant.tenant_id
    await sembrar(db_session, tid)
    await db_session.commit()
    con_n0 = await _compute_forecast(tid, db_session, 0)
    await db_session.execute(
        update(SaleEntry)
        .where(SaleEntry.tenant_id == tid, SaleEntry.payment_method == "credit_card")
        .values(payment_method="debit_card")
    )
    await db_session.commit()

    como_caja = await _compute_forecast(tid, db_session, 0)

    assert con_n0.tier == como_caja.tier == tier
    for a, b in zip(con_n0.points, como_caja.points, strict=True):
        assert abs(a.income - b.income) <= 1, (a, b)


async def test_una_venta_con_credito_de_fecha_futura_no_es_historia(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """m-8: no estira los días con datos ni entra al promedio."""
    tid = sample_tenant.tenant_id
    for i in range(20):
        db_session.add(_venta(tid, "1000", "cash", HOY - timedelta(days=i)))
    db_session.add(_venta(tid, "9000", "credit_card", HOY + timedelta(days=3)))
    await db_session.commit()

    r = await _compute_forecast(tid, db_session, 2)

    assert r.data_days == 20
    assert {p.income for p in r.points} == {1000}
    assert r.credito_sin_acreditar_ars == 0


# ── Alcance de las series ─────────────────────────────────────────────────────


async def test_alias_y_mayusculas_del_credito_van_al_credito(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    for metodo, importe in (("cash", "100"), ("tarjeta_credito", "200"), ("CREDIT_CARD", "300")):
        db_session.add(_venta(tid, importe, metodo, HOY))
    await db_session.commit()

    desde = HOY - timedelta(days=1)
    assert await ingresos_de_caja_por_dia(tid, db_session, desde) == {HOY: 100.0}
    assert await credito_por_dia(tid, db_session, desde) == {HOY: 500.0}


async def test_en_un_pago_mixto_entra_lo_liquido_y_la_parte_con_credito_va_al_credito(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """B9: $600 en efectivo + $400 con crédito → $600 entran hoy; los $400 son
    crédito de hoy, que entra N días después. Nunca la línea entera."""
    tid = sample_tenant.tenant_id
    await _operacion(
        db_session, tid, lineas=["1000"], tenders=[("cash", "600"), ("credit_card", "400")]
    )
    await db_session.commit()

    desde = HOY - timedelta(days=10)
    assert await ingresos_de_caja_por_dia(tid, db_session, desde) == {HOY: 600.0}
    assert await credito_por_dia(tid, db_session, desde) == {HOY: 400.0}


async def test_un_blob_de_la_version_anterior_no_se_sirve(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    fake_redis: Any,
) -> None:
    """La clave pasó a v4 y lleva N: el blob v3 (crédito afuera) no se lee."""
    tid = sample_tenant.tenant_id
    assert _cache_key(tid, 5) != _cache_key(tid, 14)
    assert _cache_key(tid, 14) == f"forecast:cash:v4:{tid}:n14"
    await _sembrar_efectivo_y_un_credito(db_session, tid)
    viejo = {
        "tier": 1, "confidence": "LOW", "data_days": 20, "horizon_days": 14,
        "points": [{"date": HOY.isoformat(), "income": 1, "expense": 600, "net": -599}],
        "message": None, "credito_sin_acreditar_ars": 7000.0,
    }
    await fake_redis.set(f"forecast:cash:v3:{tid}", json.dumps(viejo))

    resp = await client.get(URL, headers=auth_headers)

    assert resp.status_code == 200, resp.text
    assert 1 not in {p["income"] for p in resp.json()["points"]}


# ── G-2: frases de tarjeta en la entrada ──────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "esperado"),
    [
        ("Tarjeta de crédito", "credit_card"),
        ("tarjeta de credito", "credit_card"),
        ("TARJETA DE CRÉDITO", "credit_card"),
        ("Crédito 3 cuotas", "credit_card"),
        ("Visa crédito", "credit_card"),
        ("Mastercard Credito", "credit_card"),
        ("TC crédito", "credit_card"),
        ("Tarjeta a crédito", "credit_card"),
        ("Visa (crédito)", "credit_card"),
        ("Tarjeta de débito", "debit_card"),
        ("débito", "debit_card"),
        ("Visa débito", "debit_card"),
        ("Visa Debito", "debit_card"),
        # Ambiguos: sin «crédito» ni «débito» no se adivina.
        ("Tarjeta", "other"),
        ("Visa", "other"),
        ("crédito o débito", "other"),
        ("Nota de crédito", "other"),
        ("Nota de débito", "other"),
        ("Crédito fiscal", "other"),
        ("Crédito fiscal IVA", "other"),
        # Sin contexto de tarjeta, «crédito» no es tarjeta (N-2).
        ("Crédito personal", "other"),
        # Fiado (N-2): al canónico del fiado, el que dispara el guard del import.
        ("Fiado a crédito", "account"),
        ("Cuenta corriente crédito", "account"),
        ("Cta. cte. a crédito", "account"),
        ("Crédito en cuenta corriente", "account"),
        ("Crédito de la casa", "account"),
        ("Libreta crédito", "account"),
        ("A crédito", "account"),
        ("Venta a crédito", "account"),
        ("Fiado (crédito)", "account"),
        # Ya existentes: no cambian.
        ("débito automático", "transfer"),
        ("Débito automático de la tarjeta", "transfer"),
        # «crédito» sola: el mapa de antes del PR.
        ("credito", "credit_card"),
        ("Crédito", "credit_card"),
        ("efectivo", "cash"),
    ],
)
def test_normaliza_las_frases_de_tarjeta(raw: str, esperado: str) -> None:
    assert normalize_payment_method(raw) == esperado


async def test_una_venta_con_tarjeta_de_credito_en_castellano_no_entra_como_caja_el_dia(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """El caso E de la revisión: antes «Tarjeta de crédito» se guardaba `other`
    y entraba como caja ese día."""
    tid = sample_tenant.tenant_id
    db_session.add(_venta(tid, "700", normalize_payment_method("Tarjeta de crédito"), HOY))
    await db_session.commit()

    desde = HOY - timedelta(days=1)
    assert await ingresos_de_caja_por_dia(tid, db_session, desde) == {}
    assert await credito_por_dia(tid, db_session, desde) == {HOY: 700.0}
