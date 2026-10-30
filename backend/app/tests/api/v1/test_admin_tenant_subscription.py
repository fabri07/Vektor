"""Panel SUPERADMIN, F3: acciones comerciales por API sobre `subscription_billing_service`."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.tenant import (
    PlanDefinition,
    Subscription,
    SubscriptionPayment,
    Tenant,
)
from app.persistence.models.user import User
from app.utils.security import create_access_token, hash_password

_REF = "TRF-0001"


def _url(tenant: Tenant, accion: str) -> str:
    return f"/api/v1/admin/tenants/{tenant.tenant_id}/subscription/{accion}"


def _pago(**extra: Any) -> dict[str, Any]:
    cuerpo: dict[str, Any] = {
        "expected": "activate",
        "reference": _REF,
        "amount_ars": "40500.00",
        "exchange_rate": "1500.00",
        "plan_code": "control",
    }
    cuerpo.update(extra)
    return cuerpo


def _codigo(respuesta: Any) -> str | None:
    detalle = respuesta.json().get("detail")
    return detalle.get("code") if isinstance(detalle, dict) else None


@pytest.fixture
async def planes(db_session: AsyncSession) -> None:
    for codigo, plazas, ia in (("esencial", 1, 20), ("control", 3, 70), ("direccion", 6, 200)):
        db_session.add(
            PlanDefinition(
                plan_code=codigo,
                display_name=codigo.title(),
                seats_included=plazas,
                ia_queries_per_month=ia,
                imports_per_month=ia // 4,
                photo_pdf_reads_per_month=ia // 10,
                price_usd_reference=Decimal("27"),
                price_ars_reference=Decimal("41500"),
                is_offered=True,
            )
        )
    await db_session.commit()


@pytest.fixture
async def en_prueba(db_session: AsyncSession, sample_tenant: Tenant, planes: None) -> Subscription:
    """El FREE del fixture pasa a una prueba: de ahí sale un alta real."""
    await db_session.execute(
        update(Subscription)
        .where(Subscription.tenant_id == sample_tenant.tenant_id)
        .values(
            plan_code="control",
            status="TRIAL",
            trial_ends_at=datetime.now(UTC) + timedelta(days=14),
        )
    )
    await db_session.commit()
    db_session.expunge_all()
    return (
        await db_session.execute(
            select(Subscription).where(Subscription.tenant_id == sample_tenant.tenant_id)
        )
    ).scalar_one()


async def _con_rol(db: AsyncSession, tenant: Tenant, rol: str) -> dict[str, str]:
    user = User(
        user_id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        email=f"{rol.lower()}-{uuid.uuid4().hex[:6]}@vektor.app",
        full_name=rol,
        password_hash=hash_password("Secure789"),
        role_code=rol,
        is_active=True,
    )
    db.add(user)
    await db.commit()
    token = create_access_token(
        {"sub": str(user.user_id), "tenant_id": str(tenant.tenant_id), "role_code": rol}
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def sa(db_session: AsyncSession, sample_tenant: Tenant) -> dict[str, str]:
    return await _con_rol(db_session, sample_tenant, "SUPERADMIN")


# ── Autorización ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("rol", ["OWNER", "ADMIN", "VIEWER", "CASHIER"])
async def test_solo_el_superadmin_opera_la_suscripcion(
    client: AsyncClient, db_session: AsyncSession, sample_tenant: Tenant, rol: str
) -> None:
    h = await _con_rol(db_session, sample_tenant, rol)
    for metodo, accion, cuerpo in (
        ("post", "payments", _pago()),
        ("get", "payments", None),
        ("post", "change-plan", {"plan_code": "control"}),
        ("post", "cancel", {}),
    ):
        r = await client.request(metodo, _url(sample_tenant, accion), headers=h, json=cuerpo)
        assert r.status_code == 403, (rol, metodo, accion)


async def test_el_tenant_inexistente_es_404(
    client: AsyncClient, sa: dict[str, str], en_prueba: Subscription
) -> None:
    url = f"/api/v1/admin/tenants/{uuid.uuid4()}/subscription/payments"
    assert (await client.post(url, headers=sa, json=_pago())).status_code == 404
    assert (await client.get(url, headers=sa)).status_code == 404


# ── Activar / renovar ──────────────────────────────────────────────────────────


async def test_activar_acredita_el_pago_y_lo_atribuye_al_operador(
    client: AsyncClient,
    db_session: AsyncSession,
    sa: dict[str, str],
    sample_tenant: Tenant,
    en_prueba: Subscription,
) -> None:
    r = await client.post(_url(sample_tenant, "payments"), headers=sa, json=_pago())
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert cuerpo["plan_code"] == "control"
    assert cuerpo["already_applied"] is False

    pago = (await db_session.execute(select(SubscriptionPayment))).scalar_one()
    assert pago.reference == _REF
    assert pago.operator.startswith("panel:")
    evento = (
        await db_session.execute(
            select(DecisionAuditLog).where(
                DecisionAuditLog.decision_type == "SUBSCRIPTION_PAYMENT_APPLIED"
            )
        )
    ).scalar_one()
    assert evento.triggered_by == pago.operator


async def test_repetir_la_referencia_no_acredita_dos_veces(
    client: AsyncClient,
    db_session: AsyncSession,
    sa: dict[str, str],
    sample_tenant: Tenant,
    en_prueba: Subscription,
) -> None:
    primero = await client.post(_url(sample_tenant, "payments"), headers=sa, json=_pago())
    segundo = await client.post(_url(sample_tenant, "payments"), headers=sa, json=_pago())
    assert segundo.status_code == 200
    assert segundo.json()["already_applied"] is True
    assert segundo.json()["period_end"] == primero.json()["period_end"]
    cantidad = await db_session.scalar(select(func.count()).select_from(SubscriptionPayment))
    assert cantidad == 1


async def test_la_misma_referencia_con_otros_datos_es_409_y_no_aplica_nada(
    client: AsyncClient,
    db_session: AsyncSession,
    sa: dict[str, str],
    sample_tenant: Tenant,
    en_prueba: Subscription,
) -> None:
    await client.post(_url(sample_tenant, "payments"), headers=sa, json=_pago())
    r = await client.post(
        _url(sample_tenant, "payments"), headers=sa, json=_pago(exchange_rate="400.00")
    )
    assert r.status_code == 409
    assert _codigo(r) == "REFERENCE_MISMATCH"
    assert await db_session.scalar(select(func.count()).select_from(SubscriptionPayment)) == 1


async def test_activar_a_quien_ya_tiene_un_periodo_pago_dice_que_corresponde_renovar(
    client: AsyncClient, sa: dict[str, str], sample_tenant: Tenant, en_prueba: Subscription
) -> None:
    await client.post(_url(sample_tenant, "payments"), headers=sa, json=_pago())
    r = await client.post(
        _url(sample_tenant, "payments"), headers=sa, json=_pago(reference="TRF-0002")
    )
    assert r.status_code == 409
    assert _codigo(r) == "USE_RENEW"


async def test_renovar_extiende_el_acceso(
    client: AsyncClient, sa: dict[str, str], sample_tenant: Tenant, en_prueba: Subscription
) -> None:
    activado = await client.post(_url(sample_tenant, "payments"), headers=sa, json=_pago())
    r = await client.post(
        _url(sample_tenant, "payments"),
        headers=sa,
        json=_pago(expected="renew", reference="TRF-0002", plan_code=None),
    )
    assert r.status_code == 200, r.text
    assert r.json()["period_end"] > activado.json()["period_end"]


async def test_activar_sin_plan_o_con_un_plan_inexistente_es_422(
    client: AsyncClient, sa: dict[str, str], sample_tenant: Tenant, en_prueba: Subscription
) -> None:
    sin_plan = await client.post(
        _url(sample_tenant, "payments"), headers=sa, json=_pago(plan_code=None)
    )
    assert (sin_plan.status_code, _codigo(sin_plan)) == (422, "PLAN_REQUIRED")
    raro = await client.post(
        _url(sample_tenant, "payments"), headers=sa, json=_pago(plan_code="inexistente")
    )
    assert (raro.status_code, _codigo(raro)) == (422, "PLAN_UNKNOWN")


@pytest.mark.parametrize(
    "extra",
    [{"exchange_rate": "0"}, {"amount_ars": "-5"}, {"reference": "ab"}, {"expected": "regalar"}],
)
async def test_los_datos_invalidos_se_rechazan_sin_tocar_nada(
    client: AsyncClient,
    db_session: AsyncSession,
    sa: dict[str, str],
    sample_tenant: Tenant,
    en_prueba: Subscription,
    extra: dict[str, str],
) -> None:
    r = await client.post(_url(sample_tenant, "payments"), headers=sa, json=_pago(**extra))
    assert r.status_code == 422
    assert await db_session.scalar(select(func.count()).select_from(SubscriptionPayment)) == 0


# ── Cambiar plan / cancelar ────────────────────────────────────────────────────


async def test_cambiar_el_plan_programa_la_proxima_renovacion(
    client: AsyncClient,
    db_session: AsyncSession,
    sa: dict[str, str],
    sample_tenant: Tenant,
    en_prueba: Subscription,
) -> None:
    await client.post(_url(sample_tenant, "payments"), headers=sa, json=_pago())
    r = await client.post(
        _url(sample_tenant, "change-plan"), headers=sa, json={"plan_code": "direccion"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["scheduled_plan_code"] == "direccion"
    sub = (
        await db_session.execute(
            select(Subscription)
            .where(Subscription.tenant_id == sample_tenant.tenant_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert sub.next_plan_code == "direccion"
    assert sub.plan_code == "control"  # el período en curso no se toca


async def test_cambiar_el_plan_sin_periodo_pago_es_409(
    client: AsyncClient, sa: dict[str, str], sample_tenant: Tenant, en_prueba: Subscription
) -> None:
    r = await client.post(
        _url(sample_tenant, "change-plan"), headers=sa, json={"plan_code": "direccion"}
    )
    assert (r.status_code, _codigo(r)) == (409, "NO_PAID_PERIOD")


async def test_cancelar_conserva_el_acceso_ya_pago(
    client: AsyncClient,
    db_session: AsyncSession,
    sa: dict[str, str],
    sample_tenant: Tenant,
    en_prueba: Subscription,
) -> None:
    pago = await client.post(_url(sample_tenant, "payments"), headers=sa, json=_pago())
    r = await client.post(_url(sample_tenant, "cancel"), headers=sa, json={"reason": "se va"})
    assert r.status_code == 200, r.text
    assert r.json()["access_until"] == pago.json()["period_end"]
    sub = (
        await db_session.execute(
            select(Subscription)
            .where(Subscription.tenant_id == sample_tenant.tenant_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert sub.cancel_at_period_end is True


async def test_cancelar_sin_periodo_pago_es_409(
    client: AsyncClient, sa: dict[str, str], sample_tenant: Tenant, en_prueba: Subscription
) -> None:
    r = await client.post(_url(sample_tenant, "cancel"), headers=sa, json={})
    assert (r.status_code, _codigo(r)) == (409, "NO_PAID_PERIOD")


# ── Pagos ──────────────────────────────────────────────────────────────────────


async def test_el_listado_de_pagos_trae_lo_acreditado(
    client: AsyncClient, sa: dict[str, str], sample_tenant: Tenant, en_prueba: Subscription
) -> None:
    await client.post(_url(sample_tenant, "payments"), headers=sa, json=_pago())
    r = await client.get(_url(sample_tenant, "payments"), headers=sa)
    assert r.status_code == 200, r.text
    (item,) = r.json()["items"]
    assert item["reference"] == _REF
    assert item["plan_code"] == "control"
    assert Decimal(item["amount_usd"]) == Decimal("27")
    # El USD lo deriva el servidor de ARS / cotización, y la cotización queda anotada.
    assert Decimal(item["amount_ars"]) == Decimal("40500")
    assert Decimal(item["exchange_rate"]) == Decimal("1500")


async def test_el_usd_se_redondea_a_centavos_desde_la_cotizacion(
    client: AsyncClient, sa: dict[str, str], sample_tenant: Tenant, en_prueba: Subscription
) -> None:
    r = await client.post(
        _url(sample_tenant, "payments"),
        headers=sa,
        json=_pago(amount_ars="41500.00", exchange_rate="1537.00"),
    )
    assert r.status_code == 200, r.text
    (item,) = (await client.get(_url(sample_tenant, "payments"), headers=sa)).json()["items"]
    assert Decimal(item["amount_usd"]) == Decimal("27.00")


async def test_un_pago_sin_cotizacion_se_rechaza(
    client: AsyncClient, sa: dict[str, str], sample_tenant: Tenant, en_prueba: Subscription
) -> None:
    cuerpo = _pago()
    del cuerpo["exchange_rate"]
    r = await client.post(_url(sample_tenant, "payments"), headers=sa, json=cuerpo)
    assert r.status_code == 422
