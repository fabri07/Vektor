"""D-3 (resto, R-10) — la tarjeta de crédito no es ingreso de caja el día de la venta.

Revisión profunda: la proyección de caja (`GET /forecast/cash`) ya sacaba el
fiado y contaba una sola vez los cobros (T1, #174), pero seguía sumando la venta
con tarjeta de crédito el día que se hacía, aunque esa plata se acredita
después. FactsService la saca de la caja (`DELAYED_METHODS`). Decisión
conservadora: no se corre a una fecha de acreditación (no hay plazo cargado y
uno fijo sería inventar la fecha); queda afuera y la respuesta dice cuánto.
"""

from __future__ import annotations

import json
from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Any

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.forecast_service import (
    _cache_key,
    credito_sin_acreditar,
    ingresos_de_caja_por_dia,
)
from app.domain.business_time import today_ar
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.tests.integration.test_pago_mixto_lectores import _operacion

HOY = today_ar()
DIAS = 20
URL = "/api/v1/forecast/cash"


def _venta(tid: Any, importe: str, metodo: str, dia: Any) -> SaleEntry:
    return SaleEntry(
        tenant_id=tid,
        amount=Decimal(importe),
        quantity=1,
        transaction_date=datetime.combine(dia, time(12, 0)),
        payment_method=metodo,
        provenance="REAL",
    )


async def _sembrar_20_dias(db: AsyncSession, tid: Any) -> None:
    """Cada día: $1.000 en efectivo, $500 con crédito y $600 de gasto."""
    for i in range(DIAS):
        dia = HOY - timedelta(days=i)
        db.add(_venta(tid, "1000", "cash", dia))
        db.add(_venta(tid, "500", "credit_card", dia))
        db.add(
            ExpenseEntry(
                tenant_id=tid,
                amount=Decimal("600"),
                category="OTHER",
                transaction_date=datetime.combine(dia, time(12, 0)),
                description="Gasto diario",
                is_recurring=False,
                payment_method="cash",
            )
        )
    await db.commit()


async def test_la_proyeccion_no_cuenta_el_credito_y_dice_cuanto_dejo_afuera(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    await _sembrar_20_dias(db_session, tid)

    resp = await client.get(URL, params={"refresh": "true"}, headers=auth_headers)

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["tier"] == 1
    # Sólo el efectivo entra a caja: $1.000 por día, nunca $1.500.
    assert {p["income"] for p in data["points"]} == {1000}
    assert {p["net"] for p in data["points"]} == {400}
    assert data["credit_card_pending_ars"] == 10000.0
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
    assert vivas == DIAS


async def test_sin_credito_la_respuesta_dice_cero(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    db_session.add(_venta(sample_tenant.tenant_id, "1000", "cash", HOY))
    await db_session.commit()

    resp = await client.get(URL, params={"refresh": "true"}, headers=auth_headers)

    assert resp.status_code == 200, resp.text
    assert resp.json()["credit_card_pending_ars"] == 0.0


async def test_alias_y_mayusculas_del_credito_tambien_quedan_afuera(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    for metodo, importe in (("cash", "100"), ("tarjeta_credito", "200"), ("CREDIT_CARD", "300")):
        db_session.add(_venta(tid, importe, metodo, HOY))
    await db_session.commit()

    desde = HOY - timedelta(days=1)
    assert await ingresos_de_caja_por_dia(tid, db_session, desde) == {HOY: 100.0}
    assert await credito_sin_acreditar(tid, db_session, desde) == 500.0


async def test_en_un_pago_mixto_entra_lo_liquido_y_no_la_parte_con_credito(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """B9: $600 en efectivo + $400 con crédito → entran $600 a caja; los $400
    quedan como crédito sin acreditar, nunca la línea entera."""
    tid = sample_tenant.tenant_id
    await _operacion(
        db_session, tid, lineas=["1000"], tenders=[("cash", "600"), ("credit_card", "400")]
    )
    await db_session.commit()

    desde = HOY - timedelta(days=10)
    assert sum((await ingresos_de_caja_por_dia(tid, db_session, desde)).values()) == 600.0
    assert await credito_sin_acreditar(tid, db_session, desde) == 400.0


async def test_un_blob_de_la_version_anterior_no_se_sirve(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    fake_redis: Any,
) -> None:
    """La clave pasó a v3: el blob v2 (con el crédito adentro) no se lee."""
    tid = sample_tenant.tenant_id
    assert _cache_key(tid) == f"forecast:cash:v3:{tid}"
    await _sembrar_20_dias(db_session, tid)
    viejo = {
        "tier": 1, "confidence": "LOW", "data_days": 20, "horizon_days": 14,
        "points": [{"date": HOY.isoformat(), "income": 1500, "expense": 600, "net": 900}],
        "message": None,
    }
    await fake_redis.set(f"forecast:cash:v2:{tid}", json.dumps(viejo))

    resp = await client.get(URL, headers=auth_headers)

    assert resp.status_code == 200, resp.text
    assert {p["income"] for p in resp.json()["points"]} == {1000}
