"""D·2 — capital de los socios: alta, listado, anulación y profit-withdrawal reapuntado.

Además de los permisos y la auditoría, estos tests fijan la frontera de la
decisión: un retiro de capital NO es gasto (no aparece en FactsService ni en
`expense_entries`), pero la plata SÍ salió de la caja, así que el arqueo, la
liquidez por flujo y la proyección de caja quedan IGUAL que cuando el retiro
era un gasto PAYROLL.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.cash_close_service import CashCloseService
from app.application.services.facts_provider import build_facts_service
from app.application.services.facts_service import Period
from app.application.services.forecast_service import _compute_forecast
from app.application.services.pin_service import PinService
from app.domain.business_time import today_ar
from app.domain.verticals import Vertical
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.owner_capital import OwnerCapitalMovement
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.persistence.models.user import User
from app.persistence.repositories.owner_capital_repository import flujo_de_dinero
from app.schemas.transaction import ExpenseEntryResponse
from app.state.business_state_service import compute_business_state
from app.utils.security import create_access_token, hash_password

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("mock_score_trigger")]

URL = "/api/v1/capital-movements"
WITHDRAWAL_URL = "/api/v1/expenses/profit-withdrawal"


def _mov(kind: str = "aporte", amount: str = "1000.00", **extra: Any) -> dict[str, Any]:
    return {
        "kind": kind,
        "amount": amount,
        "occurred_on": today_ar().isoformat(),
        "payment_method": "cash",
        **extra,
    }


async def _headers_de(
    db_session: AsyncSession,
    tenant: Tenant,
    fake_redis: Any,
    role: str,
    *,
    can_modify_sensitive: bool = False,
) -> dict[str, str]:
    user = User(
        user_id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        email=f"{role.lower()}-{uuid.uuid4().hex[:6]}@kiosco.com",
        full_name=role,
        password_hash=hash_password("Secure123"),
        role_code=role,
        is_active=True,
        can_modify_sensitive=can_modify_sensitive,
        pin_hash=hash_password("1234"),
    )
    db_session.add(user)
    await db_session.commit()
    await fake_redis.set(PinService._window_key(tenant.tenant_id, user.user_id), "1", ex=600)
    token = create_access_token(
        {"sub": str(user.user_id), "tenant_id": str(tenant.tenant_id), "role_code": role}
    )
    return {"Authorization": f"Bearer {token}"}


async def _audits(db_session: AsyncSession, decision_type: str) -> list[DecisionAuditLog]:
    rows = await db_session.execute(
        select(DecisionAuditLog).where(DecisionAuditLog.decision_type == decision_type)
    )
    return list(rows.scalars().all())


async def _count(db_session: AsyncSession, model: Any) -> int:
    return int((await db_session.execute(select(func.count()).select_from(model))).scalar_one())


# ── Alta, listado, anulación ─────────────────────────────────────────────────


async def test_alta_persiste_con_el_tenant_del_jwt_y_audita(
    client: AsyncClient, auth_headers: dict[str, str], sample_tenant: Tenant,
    db_session: AsyncSession,
) -> None:
    resp = await client.post(
        URL,
        json=_mov("aporte", "100000.00", partner_name="  Juan  ", tenant_id=str(uuid.uuid4())),
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["kind"] == "aporte"
    assert body["form"] == "dinero"
    assert body["amount_ars"] == 100000.0
    assert body["partner_name"] == "Juan"
    assert body["source_type"] == "manual"

    mov = await db_session.get(OwnerCapitalMovement, uuid.UUID(body["id"]))
    assert mov is not None
    assert mov.tenant_id == sample_tenant.tenant_id  # el del JWT, no el del body

    (audit,) = await _audits(db_session, "OWNER_CAPITAL_MOVEMENT_CREATED")
    assert audit.tenant_id == sample_tenant.tenant_id
    assert audit.decision_data["record_id"] == body["id"]
    assert audit.decision_data["after"]["amount_ars"] == "100000.00"


async def test_listado_filtra_por_fecha_y_tipo(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    hoy = today_ar()
    hace_10 = (hoy - timedelta(days=10)).isoformat()
    for payload in (
        _mov("aporte", "500.00"),
        _mov("retiro", "200.00"),
        _mov("aporte", "300.00", occurred_on=hace_10),
    ):
        assert (await client.post(URL, json=payload, headers=auth_headers)).status_code == 201

    todos = (await client.get(URL, headers=auth_headers)).json()["items"]
    assert len(todos) == 3
    assert todos[0]["occurred_on"] == hoy.isoformat()  # más reciente primero

    aportes = (await client.get(URL, params={"kind": "aporte"}, headers=auth_headers)).json()
    assert sorted(i["amount_ars"] for i in aportes["items"]) == [300.0, 500.0]

    recientes = (
        await client.get(
            URL, params={"from_date": (hoy - timedelta(days=5)).isoformat()}, headers=auth_headers
        )
    ).json()["items"]
    assert sorted(i["amount_ars"] for i in recientes) == [200.0, 500.0]

    viejos = (await client.get(URL, params={"to_date": hace_10}, headers=auth_headers)).json()
    assert [i["amount_ars"] for i in viejos["items"]] == [300.0]


async def test_anulacion_no_borra_audita_y_no_se_repite(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_user: User,
) -> None:
    mid = (await client.post(URL, json=_mov(), headers=auth_headers)).json()["id"]

    resp = await client.post(f"{URL}/{mid}/void", json={"reason": "cargado dos veces"},
                             headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["voided_at"] is not None
    assert resp.json()["void_reason"] == "cargado dos veces"

    mov = await db_session.get(OwnerCapitalMovement, uuid.UUID(mid))
    assert mov is not None and mov.voided_by_user_id == sample_user.user_id

    (audit,) = await _audits(db_session, "OWNER_CAPITAL_MOVEMENT_VOIDED")
    assert audit.decision_data["before"]["voided_at"] is None
    assert audit.decision_data["after"]["void_reason"] == "cargado dos veces"

    otra = await client.post(f"{URL}/{mid}/void", json={"reason": "otra vez"},
                             headers=auth_headers)
    assert otra.status_code == 409
    assert otra.json()["detail"]["code"] == "CAPITAL_MOVEMENT_ALREADY_VOIDED"

    assert (await client.get(URL, headers=auth_headers)).json()["items"] == []
    con_anulados = await client.get(URL, params={"include_voided": True}, headers=auth_headers)
    assert [i["id"] for i in con_anulados.json()["items"]] == [mid]


async def test_el_recalculo_del_score_sale_despues_del_commit(
    client: AsyncClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Alta, anulación y profit-withdrawal agendan el recálculo para DESPUÉS del
    commit, sacándolo del camino de la respuesta. Con ``.delay()`` directo el
    worker podía leer la base antes del commit y persistir un score sin el
    movimiento, y un broker caído hacía rollback de un movimiento válido."""
    from app.api.v1 import expenses as expenses_api
    from app.api.v1 import owner_capital as capital_api

    llamadas: list[tuple[str, bool]] = []

    def _registrar(_db: Any, _tenant: str, triggered_by: str, background: Any = None) -> None:
        llamadas.append((triggered_by, background is not None))

    monkeypatch.setattr(capital_api, "trigger_score_recalculation_after_commit", _registrar)
    monkeypatch.setattr(expenses_api, "trigger_score_recalculation_after_commit", _registrar)

    mid = (await client.post(URL, json=_mov(), headers=auth_headers)).json()["id"]
    await client.post(f"{URL}/{mid}/void", json={"reason": "x"}, headers=auth_headers)
    retiro = await client.post(
        WITHDRAWAL_URL,
        json={"amount": "500.00", "withdrawal_date": today_ar().isoformat(),
              "payment_method": "cash"},
        headers=auth_headers,
    )
    assert retiro.status_code == 201, retiro.text

    assert llamadas == [
        ("owner_capital_movement_created", True),
        ("owner_capital_movement_voided", True),
        ("profit_withdrawal_created", True),
    ]


async def test_anular_exige_motivo(client: AsyncClient, auth_headers: dict[str, str]) -> None:
    mid = (await client.post(URL, json=_mov(), headers=auth_headers)).json()["id"]
    resp = await client.post(f"{URL}/{mid}/void", json={"reason": "   "}, headers=auth_headers)
    assert resp.status_code == 422


# ── Tenant ───────────────────────────────────────────────────────────────────


async def test_otro_tenant_no_ve_ni_anula(
    client: AsyncClient, auth_headers: dict[str, str], second_auth_headers: dict[str, str],
    db_session: AsyncSession,
) -> None:
    mid = (await client.post(URL, json=_mov(), headers=auth_headers)).json()["id"]

    assert (await client.get(URL, headers=second_auth_headers)).json()["items"] == []
    resp = await client.post(f"{URL}/{mid}/void", json={"reason": "x"},
                             headers=second_auth_headers)
    assert resp.status_code == 404
    mov = await db_session.get(OwnerCapitalMovement, uuid.UUID(mid))
    assert mov is not None and mov.voided_at is None


# ── Permisos y PIN ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("can_modify_sensitive", [False, True])
async def test_viewer_nunca(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant, fake_redis: Any, can_modify_sensitive: bool,
) -> None:
    """Ni siquiera un VIEWER al que el dueño le dio permiso de modificar."""
    mid = (await client.post(URL, json=_mov(), headers=auth_headers)).json()["id"]
    viewer = await _headers_de(db_session, sample_tenant, fake_redis, "VIEWER",
                               can_modify_sensitive=can_modify_sensitive)
    assert (await client.post(URL, json=_mov(), headers=viewer)).status_code == 403
    assert (await client.get(URL, headers=viewer)).status_code == 403
    resp = await client.post(f"{URL}/{mid}/void", json={"reason": "x"}, headers=viewer)
    assert resp.status_code == 403
    assert await _count(db_session, OwnerCapitalMovement) == 1


async def test_cajero_nunca(
    client: AsyncClient, db_session: AsyncSession, sample_tenant: Tenant, fake_redis: Any,
) -> None:
    cajero = await _headers_de(db_session, sample_tenant, fake_redis, "CASHIER",
                               can_modify_sensitive=True)
    assert (await client.post(URL, json=_mov(), headers=cajero)).status_code == 403
    assert (await client.get(URL, headers=cajero)).status_code == 403


async def test_admin_con_permiso_puede(
    client: AsyncClient, db_session: AsyncSession, sample_tenant: Tenant, fake_redis: Any,
) -> None:
    admin = await _headers_de(db_session, sample_tenant, fake_redis, "ADMIN",
                              can_modify_sensitive=True)
    assert (await client.post(URL, json=_mov(), headers=admin)).status_code == 201
    assert (await client.get(URL, headers=admin)).status_code == 200


async def test_admin_sin_permiso_de_modificar_no_escribe(
    client: AsyncClient, db_session: AsyncSession, sample_tenant: Tenant, fake_redis: Any,
) -> None:
    admin = await _headers_de(db_session, sample_tenant, fake_redis, "ADMIN")
    assert (await client.post(URL, json=_mov(), headers=admin)).status_code == 403
    assert (await client.get(URL, headers=admin)).status_code == 200


async def test_alta_y_anulacion_piden_pin(
    client: AsyncClient, auth_headers: dict[str, str], sample_tenant: Tenant,
    sample_user: User, fake_redis: Any, db_session: AsyncSession,
) -> None:
    mid = (await client.post(URL, json=_mov(), headers=auth_headers)).json()["id"]
    await fake_redis.delete(PinService._window_key(sample_tenant.tenant_id, sample_user.user_id))

    alta = await client.post(URL, json=_mov(), headers=auth_headers)
    assert alta.status_code == 428
    assert alta.json()["detail"] == "PIN_REQUIRED"
    baja = await client.post(f"{URL}/{mid}/void", json={"reason": "x"}, headers=auth_headers)
    assert baja.status_code == 428
    assert await _count(db_session, OwnerCapitalMovement) == 1


# ── Validación ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("amount", ["0", "-10.00", "NaN"])
async def test_importe_no_positivo_rechazado(
    client: AsyncClient, auth_headers: dict[str, str], amount: str
) -> None:
    resp = await client.post(URL, json=_mov(amount=amount), headers=auth_headers)
    assert resp.status_code == 422


async def test_fecha_futura_rechazada(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    manana = (today_ar() + timedelta(days=1)).isoformat()
    resp = await client.post(URL, json=_mov(occurred_on=manana), headers=auth_headers)
    assert resp.status_code == 422


async def test_especie_no_se_acepta_todavia(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await client.post(URL, json=_mov(form="especie"), headers=auth_headers)
    assert resp.status_code == 422


# ── profit-withdrawal reapuntado ─────────────────────────────────────────────


async def _facts(db_session: AsyncSession, tenant: Tenant) -> dict[str, Any]:
    p = Period.last_n_days(30, today=today_ar())
    facts = await build_facts_service(
        db_session, tenant.tenant_id, p, vertical=Vertical.KIOSCO_ALMACEN
    )
    tid = str(tenant.tenant_id)
    return {
        "gastos": [(f.metric, f.value) for f in facts.gastos_por_categoria(tid, p)],
        "ventas": facts.ventas_periodo(tid, p).value,
        "margen_neto": facts.margen_neto(tid, p).value,
    }


async def test_profit_withdrawal_misma_forma_y_no_crea_gasto(
    client: AsyncClient, auth_headers: dict[str, str], sample_tenant: Tenant,
    db_session: AsyncSession,
) -> None:
    antes = await _facts(db_session, sample_tenant)

    resp = await client.post(
        WITHDRAWAL_URL,
        json={"amount": "50000.00", "withdrawal_date": today_ar().isoformat(),
              "payment_method": "transfer", "notes": "sueldo"},
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # Misma forma que cuando era un gasto: el frontend viejo no se entera.
    assert set(body) == set(ExpenseEntryResponse.model_fields)
    assert body["category"] == "PAYROLL"
    assert body["expense_type"] == "OPEX"
    assert body["amount"] == 50000.0
    assert body["payment_method"] == "transfer"
    assert body["custom_fields"] == {"profit_withdrawal": True}

    # No es gasto: ni una fila en expense_entries, y FactsService no cambia.
    assert await _count(db_session, ExpenseEntry) == 0
    assert await _facts(db_session, sample_tenant) == antes

    # Es un retiro de capital, y el id de la respuesta es el del movimiento.
    mov = await db_session.get(OwnerCapitalMovement, uuid.UUID(body["id"]))
    assert mov is not None
    assert (mov.kind, mov.form, mov.payment_method) == ("retiro", "dinero", "transfer")
    assert mov.amount_ars == Decimal("50000.00")
    assert mov.occurred_on == today_ar()
    assert mov.tenant_id == sample_tenant.tenant_id
    (audit,) = await _audits(db_session, "OWNER_CAPITAL_MOVEMENT_CREATED")
    assert audit.context == {"endpoint": "expenses/profit-withdrawal"}


async def test_profit_withdrawal_sigue_pidiendo_pin(
    client: AsyncClient, auth_headers: dict[str, str], sample_tenant: Tenant,
    sample_user: User, fake_redis: Any,
) -> None:
    await fake_redis.delete(PinService._window_key(sample_tenant.tenant_id, sample_user.user_id))
    resp = await client.post(
        WITHDRAWAL_URL,
        json={"amount": "10.00", "withdrawal_date": today_ar().isoformat()},
        headers=auth_headers,
    )
    assert resp.status_code == 428


@pytest.mark.parametrize("role", ["VIEWER", "CASHIER"])
async def test_profit_withdrawal_mismo_permiso_que_capital(
    client: AsyncClient, db_session: AsyncSession, sample_tenant: Tenant,
    fake_redis: Any, role: str,
) -> None:
    """La ruta vieja escribe capital: no puede dejar pasar a quien la nueva frena."""
    headers = await _headers_de(db_session, sample_tenant, fake_redis, role,
                                can_modify_sensitive=True)
    resp = await client.post(
        WITHDRAWAL_URL,
        json={"amount": "30000.00", "withdrawal_date": today_ar().isoformat(),
              "payment_method": "cash"},
        headers=headers,
    )
    assert resp.status_code == 403
    assert await _count(db_session, OwnerCapitalMovement) == 0


async def test_profit_withdrawal_admin_con_permiso_puede(
    client: AsyncClient, db_session: AsyncSession, sample_tenant: Tenant, fake_redis: Any,
) -> None:
    admin = await _headers_de(db_session, sample_tenant, fake_redis, "ADMIN",
                              can_modify_sensitive=True)
    resp = await client.post(
        WITHDRAWAL_URL,
        json={"amount": "10.00", "withdrawal_date": today_ar().isoformat()},
        headers=admin,
    )
    assert resp.status_code == 201


async def test_ejemplo_del_plan_aporte_y_retiro(
    client: AsyncClient, auth_headers: dict[str, str], sample_tenant: Tenant,
    db_session: AsyncSession,
) -> None:
    """Aporte $100.000 + retiro $10.000 → flujo de capital $90.000, sin ventas
    ni gastos nuevos y sin cambio en el margen."""
    antes = await _facts(db_session, sample_tenant)
    hoy = today_ar().isoformat()
    assert (await client.post(URL, json=_mov("aporte", "100000.00"),
                              headers=auth_headers)).status_code == 201
    assert (await client.post(WITHDRAWAL_URL,
                              json={"amount": "10000.00", "withdrawal_date": hoy},
                              headers=auth_headers)).status_code == 201

    flujo = await flujo_de_dinero(db_session, sample_tenant.tenant_id, today_ar(), today_ar())
    neto = sum((f.entradas - f.salidas for f in flujo), Decimal("0"))
    assert neto == Decimal("90000.00")
    assert await _count(db_session, SaleEntry) == 0
    assert await _count(db_session, ExpenseEntry) == 0
    assert await _facts(db_session, sample_tenant) == antes


# ── La caja ve el retiro igual que cuando era gasto ──────────────────────────


def _gasto_retiro_viejo(tenant: Tenant, amount: Decimal, dia: date, method: str) -> ExpenseEntry:
    """Lo que escribía profit-withdrawal antes de D·2."""
    return ExpenseEntry(
        tenant_id=tenant.tenant_id,
        amount=amount,
        category="PAYROLL",
        expense_type="OPEX",
        transaction_date=datetime.combine(dia, datetime.min.time()),
        description="Retiro de ganancias anticipadas",
        is_recurring=False,
        payment_method=method,
        custom_fields={"profit_withdrawal": True},
    )


async def test_arqueo_esperado_igual_que_con_el_gasto_viejo(
    client: AsyncClient, auth_headers: dict[str, str], sample_tenant: Tenant,
    db_session: AsyncSession,
) -> None:
    hoy = today_ar()
    svc = CashCloseService(db_session)
    db_session.add(SaleEntry(tenant_id=sample_tenant.tenant_id, amount=Decimal("80000"),
                             transaction_date=datetime.combine(hoy, datetime.min.time()),
                             payment_method="cash"))
    viejo = _gasto_retiro_viejo(sample_tenant, Decimal("30000"), hoy, "cash")
    db_session.add(viejo)
    await db_session.commit()
    con_gasto = await svc._compute_expected(sample_tenant.tenant_id, hoy)

    viejo.voided_at, viejo.void_reason = datetime.now(), "MANUAL_ADMIN_VOID"
    await db_session.commit()
    assert (await client.post(WITHDRAWAL_URL,
                              json={"amount": "30000.00", "withdrawal_date": hoy.isoformat()},
                              headers=auth_headers)).status_code == 201
    con_capital = await svc._compute_expected(sample_tenant.tenant_id, hoy)
    assert con_capital == con_gasto == (50000.0, {"cash": 50000.0})

    # Un aporte en efectivo entra al cajón; uno por transferencia, no.
    await client.post(URL, json=_mov("aporte", "5000.00"), headers=auth_headers)
    await client.post(URL, json=_mov("aporte", "7000.00", payment_method="transfer"),
                      headers=auth_headers)
    total, por_medio = await svc._compute_expected(sample_tenant.tenant_id, hoy)
    assert por_medio["cash"] == 55000.0


async def test_liquidez_por_flujo_igual_que_con_el_gasto_viejo(
    client: AsyncClient, auth_headers: dict[str, str], sample_tenant: Tenant,
    db_session: AsyncSession, fake_redis: Any,
) -> None:
    dia = today_ar() - timedelta(days=2)
    viejo = _gasto_retiro_viejo(sample_tenant, Decimal("12000"), dia, "cash")
    db_session.add(viejo)
    await db_session.commit()
    con_gasto = await compute_business_state(sample_tenant.tenant_id, db_session, fake_redis)

    viejo.voided_at, viejo.void_reason = datetime.now(), "MANUAL_ADMIN_VOID"
    await db_session.commit()
    assert (await client.post(WITHDRAWAL_URL,
                              json={"amount": "12000.00", "withdrawal_date": dia.isoformat()},
                              headers=auth_headers)).status_code == 201
    # El aporte no infla la liquidez (no se cuenta como entrada líquida).
    await client.post(URL, json=_mov("aporte", "99999.00"), headers=auth_headers)
    con_capital = await compute_business_state(sample_tenant.tenant_id, db_session, fake_redis)

    assert con_capital.liquid_outflow_est == con_gasto.liquid_outflow_est == Decimal("12000")
    assert con_capital.liquid_inflow_est == con_gasto.liquid_inflow_est


async def test_un_retiro_nuevo_invalida_el_cache_del_estado(
    client: AsyncClient, auth_headers: dict[str, str], sample_tenant: Tenant,
    db_session: AsyncSession, fake_redis: Any,
) -> None:
    primero = await compute_business_state(sample_tenant.tenant_id, db_session, fake_redis)
    assert primero.liquid_outflow_est == Decimal("0")
    await client.post(URL, json=_mov("retiro", "4000.00"), headers=auth_headers)
    segundo = await compute_business_state(sample_tenant.tenant_id, db_session, fake_redis)
    assert segundo.liquid_outflow_est == Decimal("4000.00")


async def test_proyeccion_de_caja_igual_que_con_el_gasto_viejo(
    client: AsyncClient, auth_headers: dict[str, str], sample_tenant: Tenant,
    db_session: AsyncSession,
) -> None:
    hoy = today_ar()
    for d in (20, 10, 1):
        db_session.add(SaleEntry(
            tenant_id=sample_tenant.tenant_id, amount=Decimal("10000"),
            transaction_date=datetime.combine(hoy - timedelta(days=d), datetime.min.time()),
            payment_method="cash",
        ))
    dia = hoy - timedelta(days=5)
    viejo = _gasto_retiro_viejo(sample_tenant, Decimal("6000"), dia, "transfer")
    db_session.add(viejo)
    await db_session.commit()
    con_gasto = await _compute_forecast(sample_tenant.tenant_id, db_session)

    viejo.voided_at, viejo.void_reason = datetime.now(), "MANUAL_ADMIN_VOID"
    await db_session.commit()
    assert (await client.post(WITHDRAWAL_URL,
                              json={"amount": "6000.00", "withdrawal_date": dia.isoformat(),
                                    "payment_method": "transfer"},
                              headers=auth_headers)).status_code == 201
    await client.post(URL, json=_mov("aporte", "50000.00"), headers=auth_headers)
    con_capital = await _compute_forecast(sample_tenant.tenant_id, db_session)

    assert con_gasto.points, con_gasto.message
    assert con_capital == con_gasto
