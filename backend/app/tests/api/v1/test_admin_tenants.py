"""Panel SUPERADMIN, F2: `/admin/tenants` y la excepción de la caja offline.

Contrato: `docs/plans/superadmin-panel.md` (§5 caja offline).
"""

from __future__ import annotations

import unittest.mock
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.business_time import today_ar
from app.domain.pos_terminal import TERMINAL_HEADER
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.pos_operation import PosOperation
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.utils.security import create_access_token, hash_password

_BASE = "/api/v1/admin/tenants"
_FECHA = f"{today_ar().isoformat()}T10:00:00"
# 00:30 de hoy en Argentina: nunca es "futuro" para el validador (compara por día).
_COBRADO = f"{today_ar().isoformat()}T00:30:00"
_COBRADO_VIEJO = f"{(today_ar() - timedelta(days=5)).isoformat()}T10:00:00"


@pytest.fixture(autouse=True)
def _sin_celery(mock_score_trigger):
    with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
        yield


async def _headers_con_rol(
    db: AsyncSession, tenant: Tenant, rol: str, email: str
) -> dict[str, str]:
    user = User(
        user_id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        email=email,
        full_name=f"Usuario {rol}",
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
async def superadmin_headers(db_session: AsyncSession, sample_tenant: Tenant) -> dict[str, str]:
    return await _headers_con_rol(db_session, sample_tenant, "SUPERADMIN", "super@vektor.app")


def _codigo(respuesta: Any) -> str | None:
    detalle = respuesta.json().get("detail")
    return detalle.get("code") if isinstance(detalle, dict) else None


# ── Autorización ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("rol", ["OWNER", "ADMIN", "VIEWER", "CASHIER"])
async def test_solo_el_superadmin_entra(
    client: AsyncClient, db_session: AsyncSession, sample_tenant: Tenant, rol: str
) -> None:
    headers = await _headers_con_rol(db_session, sample_tenant, rol, f"{rol.lower()}@x.com")
    tid = sample_tenant.tenant_id
    for metodo, url, cuerpo in (
        ("get", _BASE, None),
        ("get", f"{_BASE}/{tid}", None),
        ("post", f"{_BASE}/{tid}/status", {"status": "SUSPENDED", "reason": "prueba"}),
    ):
        r = await client.request(metodo, url, headers=headers, json=cuerpo)
        assert r.status_code == 403, (rol, metodo, url, r.status_code)


async def test_sin_token_es_401(client: AsyncClient) -> None:
    assert (await client.get(_BASE)).status_code == 401


# ── Listado y ficha ────────────────────────────────────────────────────────────


async def test_el_listado_trae_todos_los_tenants_con_estado_de_cuenta_y_de_suscripcion(
    client: AsyncClient,
    superadmin_headers: dict[str, str],
    sample_tenant: Tenant,
    second_tenant: Tenant,
) -> None:
    r = await client.get(_BASE, headers=superadmin_headers)
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    ids = {i["tenant_id"] for i in cuerpo["items"]}
    assert {str(sample_tenant.tenant_id), str(second_tenant.tenant_id)} <= ids
    assert cuerpo["total"] == len(cuerpo["items"])
    item = next(i for i in cuerpo["items"] if i["tenant_id"] == str(sample_tenant.tenant_id))
    # Son dos controles separados.
    assert item["account_status"] == "ACTIVE"
    assert item["subscription_status"] is not None
    assert item["users_count"] >= 1


async def test_el_listado_filtra_por_nombre_y_por_estado(
    client: AsyncClient,
    db_session: AsyncSession,
    superadmin_headers: dict[str, str],
    sample_tenant: Tenant,
    second_tenant: Tenant,
) -> None:
    second_tenant.status = "SUSPENDED"
    await db_session.commit()

    r = await client.get(_BASE, params={"account_status": "suspended"}, headers=superadmin_headers)
    assert [i["tenant_id"] for i in r.json()["items"]] == [str(second_tenant.tenant_id)]

    r = await client.get(
        _BASE, params={"q": sample_tenant.display_name[:4].upper()}, headers=superadmin_headers
    )
    assert str(sample_tenant.tenant_id) in {i["tenant_id"] for i in r.json()["items"]}


async def test_el_listado_pagina(
    client: AsyncClient,
    superadmin_headers: dict[str, str],
    sample_tenant: Tenant,
    second_tenant: Tenant,
) -> None:
    primera = (await client.get(_BASE, params={"page_size": 1}, headers=superadmin_headers)).json()
    segunda = (
        await client.get(_BASE, params={"page_size": 1, "page": 2}, headers=superadmin_headers)
    ).json()
    assert primera["total"] >= 2
    assert len(primera["items"]) == len(segunda["items"]) == 1
    assert primera["items"][0]["tenant_id"] != segunda["items"][0]["tenant_id"]


async def test_page_size_tiene_tope(
    client: AsyncClient, superadmin_headers: dict[str, str]
) -> None:
    r = await client.get(_BASE, params={"page_size": 5000}, headers=superadmin_headers)
    assert r.status_code == 422


async def test_el_costo_del_listado_no_crece_con_los_tenants(
    client: AsyncClient,
    db_session: AsyncSession,
    superadmin_headers: dict[str, str],
    sample_tenant: Tenant,
) -> None:
    consultas: list[str] = []
    engine = db_session.bind.sync_engine

    def _contar(conn: Any, cursor: Any, statement: str, *a: Any) -> None:
        consultas.append(statement)

    event.listen(engine, "before_cursor_execute", _contar)
    try:
        await client.get(_BASE, headers=superadmin_headers)
        con_pocos = len(consultas)

        for i in range(6):
            db_session.add(
                Tenant(
                    tenant_id=uuid.uuid4(),
                    legal_name=f"Negocio {i} SA",
                    display_name=f"Negocio {i}",
                    status="ACTIVE",
                )
            )
        await db_session.commit()

        consultas.clear()
        r = await client.get(_BASE, headers=superadmin_headers)
        con_muchos = len(consultas)
    finally:
        event.remove(engine, "before_cursor_execute", _contar)

    assert len(r.json()["items"]) >= 7
    assert con_muchos == con_pocos


async def test_la_ficha_trae_suscripcion_usuarios_y_eventos(
    client: AsyncClient,
    superadmin_headers: dict[str, str],
    sample_tenant: Tenant,
) -> None:
    await client.post(
        f"{_BASE}/{sample_tenant.tenant_id}/status",
        json={"status": "READ_ONLY", "reason": "falta de pago"},
        headers=superadmin_headers,
    )
    r = await client.get(f"{_BASE}/{sample_tenant.tenant_id}", headers=superadmin_headers)
    assert r.status_code == 200, r.text
    ficha = r.json()
    assert ficha["account_status"] == "READ_ONLY"
    assert ficha["status_changed_at"] is not None
    assert ficha["subscription"]["plan_code"]
    assert any(u["email"] == "super@vektor.app" for u in ficha["users"])
    assert ficha["recent_events"][0]["decision_type"] == "TENANT_STATUS_CHANGED"


async def test_la_ficha_de_un_tenant_inexistente_es_404(
    client: AsyncClient, superadmin_headers: dict[str, str]
) -> None:
    r = await client.get(f"{_BASE}/{uuid.uuid4()}", headers=superadmin_headers)
    assert r.status_code == 404


# ── Cambio de estado ───────────────────────────────────────────────────────────


async def test_cambiar_el_estado_lo_aplica_y_lo_audita_con_actor_y_motivo(
    client: AsyncClient,
    db_session: AsyncSession,
    superadmin_headers: dict[str, str],
    second_tenant: Tenant,
) -> None:
    r = await client.post(
        f"{_BASE}/{second_tenant.tenant_id}/status",
        json={"status": "SUSPENDED", "reason": "  pedido del dueño  "},
        headers=superadmin_headers,
    )
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert (cuerpo["previous_status"], cuerpo["status"], cuerpo["changed"]) == (
        "ACTIVE",
        "SUSPENDED",
        True,
    )

    await db_session.refresh(second_tenant)
    assert second_tenant.status == "SUSPENDED"
    assert second_tenant.status_changed_at is not None

    evento = (
        await db_session.execute(
            select(DecisionAuditLog).where(
                DecisionAuditLog.tenant_id == second_tenant.tenant_id,
                DecisionAuditLog.decision_type == "TENANT_STATUS_CHANGED",
            )
        )
    ).scalar_one()
    assert evento.decision_data == {
        "previous_status": "ACTIVE",
        "new_status": "SUSPENDED",
        "reason": "pedido del dueño",
    }
    assert evento.actor_user_id is not None
    assert evento.triggered_by == "superadmin"


async def test_repetir_el_cambio_es_idempotente_y_no_audita_dos_veces(
    client: AsyncClient,
    db_session: AsyncSession,
    superadmin_headers: dict[str, str],
    second_tenant: Tenant,
) -> None:
    url = f"{_BASE}/{second_tenant.tenant_id}/status"
    cuerpo = {"status": "READ_ONLY", "reason": "falta de pago"}
    primero = await client.post(url, json=cuerpo, headers=superadmin_headers)
    corte = primero.json()["status_changed_at"]
    segundo = await client.post(url, json=cuerpo, headers=superadmin_headers)

    assert segundo.status_code == 200
    assert segundo.json()["changed"] is False
    # El corte NO se mueve con un reintento: lo usa la caja offline.
    assert segundo.json()["status_changed_at"] == corte
    eventos = (
        await db_session.execute(
            select(DecisionAuditLog).where(
                DecisionAuditLog.tenant_id == second_tenant.tenant_id,
                DecisionAuditLog.decision_type == "TENANT_STATUS_CHANGED",
            )
        )
    ).scalars().all()
    assert len(eventos) == 1


@pytest.mark.parametrize(
    "cuerpo",
    [
        {"status": "BANANA", "reason": "motivo válido"},
        {"status": "SUSPENDED", "reason": "ab"},
        {"status": "SUSPENDED"},
    ],
)
async def test_estado_o_motivo_invalidos_se_rechazan_sin_tocar_nada(
    client: AsyncClient,
    db_session: AsyncSession,
    superadmin_headers: dict[str, str],
    second_tenant: Tenant,
    cuerpo: dict[str, str],
) -> None:
    r = await client.post(
        f"{_BASE}/{second_tenant.tenant_id}/status", json=cuerpo, headers=superadmin_headers
    )
    assert r.status_code == 422
    await db_session.refresh(second_tenant)
    assert second_tenant.status == "ACTIVE"


async def test_suspender_corta_al_tenant_de_verdad(
    client: AsyncClient,
    db_session: AsyncSession,
    superadmin_headers: dict[str, str],
    sample_tenant: Tenant,
    second_tenant: Tenant,
) -> None:
    """De punta a punta: lo que el panel decide lo hace cumplir F1."""
    otro = await _headers_con_rol(db_session, second_tenant, "OWNER", "otro@x.com")
    assert (await client.get("/api/v1/auth/me", headers=otro)).status_code == 200

    await client.post(
        f"{_BASE}/{second_tenant.tenant_id}/status",
        json={"status": "SUSPENDED", "reason": "prueba"},
        headers=superadmin_headers,
    )
    r = await client.get("/api/v1/auth/me", headers=otro)
    assert r.status_code == 403
    assert _codigo(r) == "TENANT_SUSPENDED"

    # El soporte sigue entrando a su propio tenant y puede reactivar.
    back = await client.post(
        f"{_BASE}/{second_tenant.tenant_id}/status",
        json={"status": "ACTIVE", "reason": "regularizó"},
        headers=superadmin_headers,
    )
    assert back.json()["changed"] is True
    assert (await client.get("/api/v1/auth/me", headers=otro)).status_code == 200


# ── Caja offline: la venta ya cobrada antes del corte (§5) ─────────────────────


async def _caja_y_producto(
    client: AsyncClient, headers: dict[str, str]
) -> tuple[dict[str, str], str]:
    alta = await client.post(
        "/api/v1/pos/terminals", json={"name": f"Caja {uuid.uuid4().hex[:4]}"}, headers=headers
    )
    assert alta.status_code == 201, alta.text
    caja = {**headers, TERMINAL_HEADER: alta.json()["secret"], "X-POS-Offline": "1"}
    prod = await client.post(
        "/api/v1/products",
        json={
            "name": f"Yerba {uuid.uuid4().hex[:5]}",
            "sale_price_ars": "100.00",
            "stock_units": 10,
            "unit_cost_ars": "50.00",
            "acquired_at": "2026-01-01T09:00:00",
        },
        headers=headers,
    )
    assert prod.status_code == 201, prod.text
    return caja, str(prod.json()["id"])


def _venta(pid: str, cobrado: str = _COBRADO) -> dict[str, Any]:
    return {
        "client_operation_id": f"op-{uuid.uuid4().hex[:16]}",
        "operation_date": _FECHA,
        "charged_at": cobrado,
        "items": [{"product_id": pid, "quantity": 1}],
        "tenders": [{"payment_method": "cash", "amount_ars": "100.00"}],
    }


async def _bloquear(
    db: AsyncSession, tenant: Tenant, estado: str, corte: datetime | None
) -> None:
    tenant.status = estado
    tenant.status_changed_at = corte
    await db.commit()


@pytest.mark.parametrize("estado", ["READ_ONLY", "SUSPENDED"])
async def test_una_venta_cobrada_antes_del_corte_se_acepta_y_queda_auditada(
    client: AsyncClient,
    db_session: AsyncSession,
    auth_headers: dict[str, str],
    sample_tenant: Tenant,
    estado: str,
) -> None:
    caja, pid = await _caja_y_producto(client, auth_headers)
    await _bloquear(db_session, sample_tenant, estado, datetime.now(UTC) + timedelta(hours=1))

    r = await client.post("/api/v1/pos/operations", json=_venta(pid), headers=caja)
    assert r.status_code == 201, r.text

    evento = (
        await db_session.execute(
            select(DecisionAuditLog).where(
                DecisionAuditLog.decision_type == "POS_OFFLINE_SALE_ACCEPTED_AFTER_STATUS_CHANGE"
            )
        )
    ).scalar_one()
    assert evento.decision_data["tenant_status"] == estado


async def test_una_venta_cobrada_despues_del_corte_se_rechaza_y_no_deja_nada(
    client: AsyncClient,
    db_session: AsyncSession,
    auth_headers: dict[str, str],
    sample_tenant: Tenant,
) -> None:
    caja, pid = await _caja_y_producto(client, auth_headers)
    await _bloquear(db_session, sample_tenant, "READ_ONLY", datetime.now(UTC) - timedelta(days=2))

    r = await client.post("/api/v1/pos/operations", json=_venta(pid), headers=caja)
    assert r.status_code == 403
    assert _codigo(r) == "TENANT_READ_ONLY"
    assert (await db_session.execute(select(PosOperation))).first() is None


async def test_sin_corte_conocido_la_excepcion_no_se_abre(
    client: AsyncClient,
    db_session: AsyncSession,
    auth_headers: dict[str, str],
    sample_tenant: Tenant,
) -> None:
    caja, pid = await _caja_y_producto(client, auth_headers)
    await _bloquear(db_session, sample_tenant, "SUSPENDED", None)

    r = await client.post("/api/v1/pos/operations", json=_venta(pid), headers=caja)
    assert r.status_code == 403
    assert _codigo(r) == "TENANT_SUSPENDED"


async def test_una_venta_demasiado_vieja_no_entra_aunque_sea_anterior_al_corte(
    client: AsyncClient,
    db_session: AsyncSession,
    auth_headers: dict[str, str],
    sample_tenant: Tenant,
) -> None:
    """`charged_at` lo declara el cliente: fechar hacia atrás sólo cubre la ventana."""
    caja, pid = await _caja_y_producto(client, auth_headers)
    await _bloquear(db_session, sample_tenant, "READ_ONLY", datetime.now(UTC))

    r = await client.post(
        "/api/v1/pos/operations", json=_venta(pid, _COBRADO_VIEJO), headers=caja
    )
    assert r.status_code == 403
    assert _codigo(r) == "TENANT_READ_ONLY"


async def test_sin_el_header_offline_una_venta_nueva_se_corta_en_la_dependencia(
    client: AsyncClient,
    db_session: AsyncSession,
    auth_headers: dict[str, str],
    sample_tenant: Tenant,
) -> None:
    """La excepción es sólo para lo que se cobró sin conexión."""
    caja, pid = await _caja_y_producto(client, auth_headers)
    caja.pop("X-POS-Offline")
    await _bloquear(db_session, sample_tenant, "READ_ONLY", datetime.now(UTC) + timedelta(hours=1))

    r = await client.post("/api/v1/pos/operations", json=_venta(pid), headers=caja)
    assert r.status_code == 403
    assert _codigo(r) == "TENANT_READ_ONLY"


async def test_el_reintento_de_una_venta_que_ya_existe_devuelve_su_ticket(
    client: AsyncClient,
    db_session: AsyncSession,
    auth_headers: dict[str, str],
    sample_tenant: Tenant,
) -> None:
    """Un reintento tras una respuesta perdida no se evalúa contra el estado (§5.3)."""
    caja, pid = await _caja_y_producto(client, auth_headers)
    cuerpo = _venta(pid)
    primera = await client.post("/api/v1/pos/operations", json=cuerpo, headers=caja)
    assert primera.status_code == 201, primera.text

    # Corte DESPUÉS de la venta pero con `charged_at` posterior al corte: de ser una
    # venta nueva se rechazaría. Como ya existe, devuelve el ticket.
    await _bloquear(db_session, sample_tenant, "SUSPENDED", datetime.now(UTC) - timedelta(days=2))
    otra = await client.post("/api/v1/pos/operations", json=cuerpo, headers=caja)
    assert otra.status_code == 200, otra.text
    assert otra.json()["client_operation_id"] == cuerpo["client_operation_id"]
