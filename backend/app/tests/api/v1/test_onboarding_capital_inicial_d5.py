"""D·5 — capital inicial opcional en el onboarding.

Es un aporte de capital ``onboarding_declarado``: lo que el dueño DICE que puso
al arrancar. El Balance lo muestra rotulado; ningún agregado de caja lo lee
(arqueo, liquidez, proyección), no suma a la completitud y no se duplica.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.cash_close_service import CashCloseService
from app.application.services.forecast_service import _compute_forecast
from app.domain.business_time import today_ar
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.owner_capital import OwnerCapitalMovement
from app.persistence.models.user import User
from app.persistence.repositories.owner_capital_repository import flujo_de_dinero
from app.state.business_state_service import compute_business_state
from app.tests.api.v1.test_onboarding import (  # noqa: F401  (fixtures autouse)
    _ONBOARDING_PAYLOAD,
    _REGISTER_PAYLOAD,
    _register_and_token,
    _registro_abierto,
    _sin_broker,
)

SUBMIT = "/api/v1/onboarding/submit"


def _con_capital(monto: Any = "500000.00", fecha: date | None = None) -> dict[str, Any]:
    return {
        **_ONBOARDING_PAYLOAD,
        "initial_capital_ars": monto,
        "initial_capital_date": (fecha or today_ar() - timedelta(days=3)).isoformat(),
    }


async def _alta(client: AsyncClient) -> dict[str, str]:
    return {"Authorization": f"Bearer {await _register_and_token(client)}"}


async def _usuario(db_session: AsyncSession) -> User:
    return (
        await db_session.execute(select(User).where(User.email == _REGISTER_PAYLOAD["email"]))
    ).scalar_one()


async def _declarados(db_session: AsyncSession, tenant_id: Any) -> list[OwnerCapitalMovement]:
    return list(
        (
            await db_session.execute(
                select(OwnerCapitalMovement).where(
                    OwnerCapitalMovement.tenant_id == tenant_id,
                    OwnerCapitalMovement.source_type == "onboarding_declarado",
                )
            )
        ).scalars()
    )


async def test_con_capital_crea_un_aporte_declarado_con_su_fecha_y_usuario(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    headers = await _alta(client)
    fecha = today_ar() - timedelta(days=40)
    resp = await client.post(SUBMIT, json=_con_capital(fecha=fecha), headers=headers)
    assert resp.status_code == 200, resp.text

    user = await _usuario(db_session)
    (mov,) = await _declarados(db_session, user.tenant_id)
    assert (mov.kind, mov.form, mov.payment_method) == ("aporte", "dinero", "other")
    assert str(mov.amount_ars) == "500000.00"
    assert mov.occurred_on == fecha
    assert mov.created_by_user_id == user.user_id
    assert mov.partner_name is None and mov.voided_at is None

    audit = (
        await db_session.execute(
            select(DecisionAuditLog).where(
                DecisionAuditLog.tenant_id == user.tenant_id,
                DecisionAuditLog.decision_type == "OWNER_CAPITAL_MOVEMENT_CREATED",
            )
        )
    ).scalar_one()
    assert audit.triggered_by == "ui:onboarding"
    assert audit.decision_data["after"]["source_type"] == "onboarding_declarado"


async def test_el_capital_no_cambia_la_completitud(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    headers = await _alta(client)
    # Sin caja declarada: 80. Con capital inicial sigue en 80 (no es un input).
    payload = {**_con_capital(), "cash_on_hand_ars": None}
    body = (await client.post(SUBMIT, json=payload, headers=headers)).json()
    assert body["data_completeness_score"] == 80


async def test_sin_capital_no_crea_nada(client: AsyncClient, db_session: AsyncSession) -> None:
    headers = await _alta(client)
    assert (await client.post(SUBMIT, json=_ONBOARDING_PAYLOAD, headers=headers)).status_code == 200
    user = await _usuario(db_session)
    assert await _declarados(db_session, user.tenant_id) == []


@pytest.mark.parametrize(
    "extra",
    [
        {"initial_capital_ars": "1000.00"},
        {"initial_capital_date": "2026-01-01"},
        {"initial_capital_ars": "0", "initial_capital_date": "2026-01-01"},
        {"initial_capital_ars": "-5", "initial_capital_date": "2026-01-01"},
        {"initial_capital_ars": "10.001", "initial_capital_date": "2026-01-01"},
    ],
    ids=["monto_sin_fecha", "fecha_sin_monto", "cero", "negativo", "tres_decimales"],
)
async def test_capital_invalido_422_y_no_completa_el_onboarding(
    client: AsyncClient, db_session: AsyncSession, extra: dict[str, str]
) -> None:
    headers = await _alta(client)
    resp = await client.post(SUBMIT, json={**_ONBOARDING_PAYLOAD, **extra}, headers=headers)
    assert resp.status_code == 422, resp.text
    # Un 422 no deja el alta a medias: se puede volver a mandar bien.
    assert (await client.post(SUBMIT, json=_ONBOARDING_PAYLOAD, headers=headers)).status_code == 200


async def test_fecha_futura_422(client: AsyncClient) -> None:
    headers = await _alta(client)
    payload = _con_capital(fecha=today_ar() + timedelta(days=1))
    assert (await client.post(SUBMIT, json=payload, headers=headers)).status_code == 422


async def test_segundo_submit_409_y_sigue_habiendo_uno(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    headers = await _alta(client)
    assert (await client.post(SUBMIT, json=_con_capital(), headers=headers)).status_code == 200
    segundo = await client.post(SUBMIT, json=_con_capital("9.00"), headers=headers)
    assert segundo.status_code == 409
    user = await _usuario(db_session)
    assert len(await _declarados(db_session, user.tenant_id)) == 1


async def test_con_un_declarado_vivo_previo_no_crea_otro(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    headers = await _alta(client)
    user = await _usuario(db_session)
    db_session.add(
        OwnerCapitalMovement(
            tenant_id=user.tenant_id, kind="aporte", form="dinero",
            amount_ars="1.00", occurred_on=today_ar(), payment_method="other",
            source_type="onboarding_declarado", created_by_user_id=user.user_id,
        )
    )
    await db_session.commit()
    assert (await client.post(SUBMIT, json=_con_capital(), headers=headers)).status_code == 200
    (unico,) = await _declarados(db_session, user.tenant_id)
    assert str(unico.amount_ars) == "1.00"


# ── La caja nunca lo ve ──────────────────────────────────────────────────────


async def test_la_caja_no_ve_el_capital_declarado(
    client: AsyncClient, db_session: AsyncSession, fake_redis: Any
) -> None:
    headers = await _alta(client)
    user = await _usuario(db_session)
    tid = user.tenant_id
    hoy = today_ar()
    svc = CashCloseService(db_session)
    arqueo_antes = await svc._compute_expected(tid, hoy)
    estado_antes = await compute_business_state(tid, db_session, fake_redis)
    proyeccion_antes = await _compute_forecast(tid, db_session)

    resp = await client.post(SUBMIT, json=_con_capital(fecha=hoy), headers=headers)
    assert resp.status_code == 200, resp.text
    fake_redis._store.clear()  # que el estado se recalcule, no salga del caché

    # Ni siquiera aparece en el flujo de capital que leen los tres.
    assert await flujo_de_dinero(db_session, tid, hoy - timedelta(days=90), hoy) == []
    assert await svc._compute_expected(tid, hoy) == arqueo_antes
    estado = await compute_business_state(tid, db_session, fake_redis)
    assert estado.liquid_outflow_est == estado_antes.liquid_outflow_est
    assert estado.liquid_inflow_est == estado_antes.liquid_inflow_est
    assert await _compute_forecast(tid, db_session) == proyeccion_antes


async def test_un_aporte_declarado_en_efectivo_tampoco_entra_al_arqueo(
    db_session: AsyncSession, sample_tenant: Any
) -> None:
    """Aunque alguien lo cargara con medio efectivo, el arqueo no lo espera."""
    hoy = today_ar()
    db_session.add(
        OwnerCapitalMovement(
            tenant_id=sample_tenant.tenant_id, kind="aporte", form="dinero",
            amount_ars="80000.00", occurred_on=hoy, payment_method="cash",
            source_type="onboarding_declarado",
        )
    )
    await db_session.commit()
    total, por_medio = await CashCloseService(db_session)._compute_expected(
        sample_tenant.tenant_id, hoy
    )
    assert (total, por_medio) == (0.0, {})


# ── El Balance lo muestra rotulado ───────────────────────────────────────────


async def test_el_balance_lo_cuenta_en_aportes_y_no_en_el_flujo(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    headers = await _alta(client)
    fecha = today_ar() - timedelta(days=3)
    resp = await client.post(SUBMIT, json=_con_capital(fecha=fecha), headers=headers)
    assert resp.status_code == 200, resp.text
    hoy = today_ar()
    resp = await client.get(
        "/api/v1/capital-movements/summary",
        params={"from_date": (hoy - timedelta(days=29)).isoformat(), "to_date": hoy.isoformat()},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    (sin_socio,) = body["partners"]
    assert sin_socio["partner_name"] is None
    assert sin_socio["aportes"]["dinero"] == 500000.0
    assert body["cash_flow"] == {"entradas": 0.0, "salidas": 0.0, "neto": 0.0}
    assert body["initial_capital_declared"] == {
        "amount_ars": 500000.0, "occurred_on": fecha.isoformat()
    }


async def test_un_viewer_no_declara_capital(client: AsyncClient, db_session: AsyncSession) -> None:
    headers = await _alta(client)
    user = await _usuario(db_session)
    user.role_code = "VIEWER"
    await db_session.commit()
    resp = await client.post(SUBMIT, json=_con_capital(), headers=headers)
    assert resp.status_code == 403, resp.text
    assert await _declarados(db_session, user.tenant_id) == []
    # Sin capital, el alta sigue abierta para él (como antes de D·5).
    assert (await client.post(SUBMIT, json=_ONBOARDING_PAYLOAD, headers=headers)).status_code == 200
