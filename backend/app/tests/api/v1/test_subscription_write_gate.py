"""Bloque B — la suscripción vencida bloquea las ALTAS, nunca las correcciones.

La clasificación es por efecto, no por verbo HTTP, y tiene que ser la misma
por API, por el agente y por el alta de usuarios. El test estructural
(`test_las_rutas_con_gate_son_exactamente_estas`) es la compuerta: agregar
una ruta de alta sin gate, o ponerle gate a una corrección, lo pone en rojo.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.routing import APIRoute
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import require_active_subscription
from app.application.agents.shared.schemas import ActionType
from app.application.services.pending_action_service import (
    WRITE_EXEMPT_ACTION_TYPES,
    execute_pending_action,
)
from app.domain.subscription import SubscriptionAccessDenied
from app.main import create_app
from app.persistence.models.pending_action import PendingAction
from app.persistence.models.tenant import Subscription, Tenant
from app.persistence.models.user import User

P = "/api/v1"

#: Altas de negocio (y lecturas con IA) que exigen suscripción habilitada.
RUTAS_CON_GATE: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", f"{P}/sales"),
        ("POST", f"{P}/sales/bulk"),
        ("POST", f"{P}/sales/manual-batch"),
        # B3. Es un alta, así que entra bajo la misma política que el resto.
        # **B14 la va a SACAR de acá**: la decisión de producto es que la caja
        # siempre vende y lo que se bloquea por suscripción vencida es el
        # análisis. Hasta entonces sigue la regla vigente, no la futura.
        ("POST", f"{P}/pos/operations"),
        ("POST", f"{P}/expenses"),
        ("POST", f"{P}/products"),
        ("POST", f"{P}/products/custom-categories"),
        ("POST", f"{P}/cash-closes"),
        ("POST", f"{P}/customers"),
        ("POST", f"{P}/customers/extract"),
        ("POST", f"{P}/customers/import/confirm"),
        ("POST", f"{P}/suppliers"),
        ("POST", f"{P}/suppliers/{{supplier_id}}/receipts"),
        ("POST", f"{P}/suppliers/{{supplier_id}}/receipts/extract"),
        ("POST", f"{P}/fields/definitions"),
        ("POST", f"{P}/automations/from-pending-action/{{pending_id}}"),
        ("POST", f"{P}/marketing/metrics"),
        ("POST", f"{P}/marketing/metrics/bulk"),
        ("POST", f"{P}/others/{{record_id}}/reclassify"),
        ("POST", f"{P}/others/{{record_id}}/resolve-purchase"),
        ("POST", f"{P}/others/bulk-import"),
        # Ingesta (bloque C): subir dispara parseo con IA; confirmar e importar
        # en segundo plano además consumen cupo. La relectura es corrección.
        ("POST", f"{P}/ingestion/upload"),
        ("POST", f"{P}/ingestion/files/{{file_id}}/confirm"),
        ("POST", f"{P}/ingestion/files/{{file_id}}/imports"),
    }
)


def _tiene_gate(route: APIRoute) -> bool:
    pendientes = list(route.dependant.dependencies)
    while pendientes:
        dep = pendientes.pop()
        if dep.call is require_active_subscription:
            return True
        pendientes.extend(dep.dependencies)
    return False


def test_las_rutas_con_gate_son_exactamente_estas() -> None:
    reales = {
        (metodo, route.path)
        for route in create_app().routes
        if isinstance(route, APIRoute) and _tiene_gate(route)
        for metodo in route.methods
    }
    assert reales == RUTAS_CON_GATE


def test_ninguna_correccion_ni_baja_lleva_gate() -> None:
    """PATCH/DELETE corrigen o dan de baja datos ya cargados: siempre abiertos."""
    assert not {m for m, _ in RUTAS_CON_GATE if m in {"PATCH", "PUT", "DELETE"}}


async def _solo_lectura(db_session: AsyncSession, tenant: Tenant, **valores: Any) -> None:
    base = {"plan_code": "control", "status": "READ_ONLY", "seats_included": 3}
    await db_session.execute(
        update(Subscription)
        .where(Subscription.tenant_id == tenant.tenant_id)
        .values(**base | valores)
    )
    await db_session.commit()


@pytest.mark.parametrize("metodo_ruta", sorted(RUTAS_CON_GATE))
async def test_en_solo_lectura_cada_alta_devuelve_402(
    metodo_ruta: tuple[str, str],
    client: AsyncClient,
    auth_headers: dict[str, str],
    sample_tenant: Tenant,
    db_session: AsyncSession,
) -> None:
    """El gate corta ANTES de validar el cuerpo: un body vacío alcanza."""
    await _solo_lectura(db_session, sample_tenant)
    _, ruta = metodo_ruta
    for nombre in ("supplier_id", "pending_id", "record_id", "file_id"):
        ruta = ruta.replace(f"{{{nombre}}}", str(uuid.uuid4()))

    resp = await client.post(ruta, json={}, headers=auth_headers)

    assert resp.status_code == 402, f"{ruta}: {resp.status_code} {resp.text}"
    assert resp.json()["detail"]["code"] == "SUBSCRIPTION_READ_ONLY"


# ── Agente: un solo embudo, clasificado por ActionType ────────────────────────

#: Lo que el agente NO puede ejecutar con la suscripción vencida. Explícito a
#: propósito: un `ActionType` nuevo tiene que entrar acá o en los exentos.
ACCIONES_BLOQUEADAS: frozenset[ActionType] = frozenset(
    {
        ActionType.REGISTER_SALE,
        ActionType.REGISTER_CASH_INFLOW,
        ActionType.REGISTER_EXPENSE,
        ActionType.REGISTER_PURCHASE,
        ActionType.REGISTER_CASH_OUTFLOW,
        ActionType.UPDATE_STOCK,
        ActionType.REGISTER_STOCK_LOSS,
        ActionType.CREATE_PURCHASE_SUGGESTION,
        ActionType.IMPORT_TABULAR_FILE,
        ActionType.PARSE_DOCUMENT_FILE,
        ActionType.CREATE_SUPPLIER_DRAFT,
        ActionType.CLASSIFY_GMAIL_MESSAGE,
        ActionType.SYNC_TO_GOOGLE,
        ActionType.CREATE_CALENDAR_EVENT,
        ActionType.UPLOAD_TO_DRIVE,
        ActionType.CREATE_GOOGLE_DOC,
        ActionType.APPEND_TO_SHEET,
    }
)


def test_todo_action_type_esta_clasificado() -> None:
    assert not ACCIONES_BLOQUEADAS & WRITE_EXEMPT_ACTION_TYPES
    assert frozenset(ActionType) == ACCIONES_BLOQUEADAS | WRITE_EXEMPT_ACTION_TYPES


def _accion(tenant: Tenant, user: User, action_type: ActionType) -> PendingAction:
    return PendingAction(
        id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        action_type=action_type,
        payload={"amount": 500, "payment_method": "cash"},
        risk_level="MEDIUM",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )


async def test_el_agente_no_registra_una_venta_en_solo_lectura(
    db_session: AsyncSession, sample_tenant: Tenant, sample_user: User
) -> None:
    """Cubre botón, confirmación por texto, grupo, auto-ejecución y retry: los
    cinco pasan por `execute_pending_action` — y la confirmación por texto no
    pasa por ningún gate HTTP."""
    await _solo_lectura(db_session, sample_tenant)
    with pytest.raises(SubscriptionAccessDenied) as exc:
        await execute_pending_action(
            _accion(sample_tenant, sample_user, ActionType.REGISTER_SALE), db_session
        )
    assert isinstance(exc.value.user_message, str)  # lo que el chat le muestra


async def test_el_agente_si_corrige_un_producto_en_solo_lectura(
    db_session: AsyncSession, sample_tenant: Tenant, sample_user: User
) -> None:
    await _solo_lectura(db_session, sample_tenant)
    # Sin product_id el handler no hace nada: alcanza con que NO lo frene el gate.
    await execute_pending_action(
        _accion(sample_tenant, sample_user, ActionType.UPDATE_PRODUCT), db_session
    )


# ── Usuarios: plazas del plan ─────────────────────────────────────────────────


def _usuario_nuevo() -> dict[str, str]:
    return {
        "email": f"nuevo+{uuid.uuid4().hex[:8]}@test.com",
        "full_name": "Usuario Nuevo",
        "role_code": "VIEWER",
        "password": "Secure1234",
    }


async def test_alta_de_usuario_respeta_las_plazas_del_plan(
    client: AsyncClient,
    auth_headers: dict[str, str],
    sample_tenant: Tenant,
    db_session: AsyncSession,
) -> None:
    ahora = datetime.now(UTC)
    await _solo_lectura(
        db_session,
        sample_tenant,
        status="ACTIVE",
        seats_included=2,
        current_period_start=ahora - timedelta(days=1),
        current_period_end=ahora + timedelta(days=29),
    )

    primera = await client.post(f"{P}/users", json=_usuario_nuevo(), headers=auth_headers)
    assert primera.status_code == 201, primera.text  # dueño + 1 = 2 plazas

    segunda = await client.post(f"{P}/users", json=_usuario_nuevo(), headers=auth_headers)
    assert segunda.status_code == 409, segunda.text
    detalle = segunda.json()["detail"]
    assert detalle == {"code": "SEAT_LIMIT_EXCEEDED", "seats_included": 2, "active_users": 2}


async def test_en_prueba_hay_una_sola_plaza_sea_cual_sea_el_plan(
    client: AsyncClient,
    auth_headers: dict[str, str],
    sample_tenant: Tenant,
    db_session: AsyncSession,
) -> None:
    await _solo_lectura(
        db_session,
        sample_tenant,
        status="TRIAL",
        seats_included=6,
        trial_ends_at=datetime.now(UTC) + timedelta(days=10),
    )
    resp = await client.post(f"{P}/users", json=_usuario_nuevo(), headers=auth_headers)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["seats_included"] == 1


async def test_free_legado_sigue_sumando_usuarios_como_antes(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    """Su `seats_included=1` es un default de columna, no una condición
    comercial: aplicarlo le cortaría algo que ya podía hacer."""
    resp = await client.post(f"{P}/users", json=_usuario_nuevo(), headers=auth_headers)
    assert resp.status_code == 201, resp.text


async def test_con_suscripcion_vencida_no_se_suman_usuarios_pero_si_se_dan_de_baja(
    client: AsyncClient,
    auth_headers: dict[str, str],
    sample_tenant: Tenant,
    db_session: AsyncSession,
) -> None:
    alta = await client.post(f"{P}/users", json=_usuario_nuevo(), headers=auth_headers)
    user_id = alta.json()["user_id"]
    await _solo_lectura(db_session, sample_tenant)

    bloqueada = await client.post(f"{P}/users", json=_usuario_nuevo(), headers=auth_headers)
    assert bloqueada.status_code == 402, bloqueada.text

    baja = await client.delete(f"{P}/users/{user_id}", headers=auth_headers)
    assert baja.status_code < 300, baja.text
