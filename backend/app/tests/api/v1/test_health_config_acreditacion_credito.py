"""D-3/R-10 — a cuántos días se cuenta la tarjeta de crédito, en `/settings/health-config`.

N vive en `business_heuristic_overrides` (`credit_card_settlement_days`, sin
migración), 0–60, por defecto 14. El PATCH es parcial: cambiar N no toca los
márgenes ni los marca como personalizados, y al revés.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.business_time import today_ar
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.heuristic_override import BusinessHeuristicOverride
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry

URL = "/api/v1/settings/health-config"
FORECAST = "/api/v1/forecast/cash"
HOY = today_ar()


async def _tipos_auditados(db: AsyncSession, tid: Any) -> list[str]:
    rows = await db.execute(
        select(DecisionAuditLog.decision_type)
        .where(DecisionAuditLog.tenant_id == tid)
        .order_by(DecisionAuditLog.created_at)
    )
    return list(rows.scalars())


async def _overrides(db: AsyncSession, tid: Any) -> dict[str, Any]:
    rows = await db.execute(
        select(BusinessHeuristicOverride).where(BusinessHeuristicOverride.tenant_id == tid)
    )
    return {r.param_key: r.param_value["value"] for r in rows.scalars()}


async def test_por_defecto_son_14_dias(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await client.get(URL, headers=auth_headers)

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["credit_card_settlement_days"] == 14
    assert data["credit_card_settlement_days_is_custom"] is False
    assert data["is_custom"] is False


async def test_cambiar_n_solo_no_toca_los_margenes(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    resp = await client.patch(URL, json={"credit_card_settlement_days": 30}, headers=auth_headers)

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["credit_card_settlement_days"] == 30
    assert data["credit_card_settlement_days_is_custom"] is True
    assert data["is_custom"] is False
    assert await _overrides(db_session, sample_tenant.tenant_id) == {
        "credit_card_settlement_days": 30
    }
    assert (await client.get(URL, headers=auth_headers)).json()["credit_card_settlement_days"] == 30
    (audit,) = (
        await db_session.execute(
            select(DecisionAuditLog).where(
                DecisionAuditLog.tenant_id == sample_tenant.tenant_id,
                DecisionAuditLog.decision_type == "CREDIT_CARD_SETTLEMENT_DAYS_UPDATED",
            )
        )
    ).scalars().all()
    assert audit.decision_data["before"]["credit_card_settlement_days"] == 14
    assert audit.decision_data["after"]["credit_card_settlement_days"] == 30


async def test_cambiar_los_margenes_conserva_n(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    await client.patch(URL, json={"credit_card_settlement_days": 0}, headers=auth_headers)

    resp = await client.patch(
        URL, json={"target_margin_pct": 40, "warning_margin_pct": 25}, headers=auth_headers
    )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert (data["target_margin_pct"], data["warning_margin_pct"], data["is_custom"]) == (
        40.0,
        25.0,
        True,
    )
    assert data["credit_card_settlement_days"] == 0
    assert await _overrides(db_session, sample_tenant.tenant_id) == {
        "credit_card_settlement_days": 0,
        "target_margin_pct": 40.0,
        "warning_margin_pct": 25.0,
    }


async def test_n_fuera_de_rango_o_un_margen_suelto_es_422(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    for body in (
        {"credit_card_settlement_days": 61},
        {"credit_card_settlement_days": -1},
        # StrictInt: ni bool ni texto ni decimales.
        {"credit_card_settlement_days": True},
        {"credit_card_settlement_days": "7"},
        {"credit_card_settlement_days": 7.5},
        {"target_margin_pct": 40},
        {},
    ):
        resp = await client.patch(URL, json=body, headers=auth_headers)
        assert resp.status_code == 422, (body, resp.text)
    assert await _overrides(db_session, sample_tenant.tenant_id) == {}


async def test_restablecer_vuelve_n_a_14(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    await client.patch(URL, json={"credit_card_settlement_days": 45}, headers=auth_headers)

    resp = await client.delete(URL, headers=auth_headers)

    assert resp.status_code == 200, resp.text
    assert resp.json()["credit_card_settlement_days"] == 14
    assert resp.json()["credit_card_settlement_days_is_custom"] is False
    assert await _overrides(db_session, sample_tenant.tenant_id) == {}


async def test_restablecer_audita_n_aparte_solo_si_cambia(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    await client.patch(
        URL, json={"target_margin_pct": 40, "warning_margin_pct": 25}, headers=auth_headers
    )
    await client.delete(URL, headers=auth_headers)
    assert await _tipos_auditados(db_session, tid) == [
        "MARGIN_CONFIG_UPDATED",
        "MARGIN_CONFIG_RESET",
    ]

    await client.patch(
        URL,
        json={"target_margin_pct": 40, "warning_margin_pct": 25, "credit_card_settlement_days": 9},
        headers=auth_headers,
    )
    await client.delete(URL, headers=auth_headers)

    assert (await _tipos_auditados(db_session, tid))[2:] == [
        "MARGIN_CONFIG_UPDATED",
        "CREDIT_CARD_SETTLEMENT_DAYS_UPDATED",
        "MARGIN_CONFIG_RESET",
        "CREDIT_CARD_SETTLEMENT_DAYS_RESET",
    ]


def _con_setex(fake_redis: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """El FakeRedis compartido no tiene `setex`: sin esto la caché nunca se escribe."""

    async def setex(key: str, ttl: int, value: str) -> None:
        await fake_redis.set(key, value, ex=ttl)

    monkeypatch.setattr(fake_redis, "setex", setex, raising=False)


def _venta(tid: Any, importe: str, dias_atras: int) -> SaleEntry:
    return SaleEntry(
        tenant_id=tid,
        amount=Decimal(importe),
        quantity=1,
        transaction_date=datetime.combine(HOY - timedelta(days=dias_atras), time(12)),
        payment_method="cash",
        provenance="REAL",
    )


async def _cachear_y_cargar_una_venta(
    client: AsyncClient, auth_headers: dict[str, str], db: AsyncSession, tid: Any
) -> None:
    """Cachea la proyección con N = 14 y después carga una venta de $50.000."""
    for i in range(20):
        db.add(_venta(tid, "1000", i))
    await db.commit()
    antes = (await client.get(FORECAST, headers=auth_headers)).json()
    assert antes["points"][0]["income"] == 1000
    db.add(_venta(tid, "50000", 0))
    await db.commit()


@pytest.mark.parametrize("metodo", ["patch", "delete"])
async def test_patch_y_delete_invalidan_la_proyeccion_cacheada(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    fake_redis: Any,
    monkeypatch: pytest.MonkeyPatch,
    metodo: str,
) -> None:
    """Guardar o restablecer con el MISMO N reusa su clave: sin invalidar, se
    servía el blob de antes del cambio."""
    _con_setex(fake_redis, monkeypatch)
    await _cachear_y_cargar_una_venta(client, auth_headers, db_session, sample_tenant.tenant_id)

    if metodo == "patch":
        await client.patch(URL, json={"credit_card_settlement_days": 14}, headers=auth_headers)
    else:
        await client.delete(URL, headers=auth_headers)
    despues = (await client.get(FORECAST, headers=auth_headers)).json()

    assert despues["credit_card_settlement_days"] == 14
    assert despues["points"][0]["income"] == 3500  # (20 × 1.000 + 50.000) / 20
