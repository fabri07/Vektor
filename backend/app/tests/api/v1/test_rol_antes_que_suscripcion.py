"""R-7 — en un alta, el rol se mira ANTES que la suscripción.

Revisión profunda 2026-10-24: las rutas con `dependencies=[Depends(
require_active_subscription)]` en el decorador y el rol como parámetro del
handler resolvían el gate primero —FastAPI corre las del decorador antes que
las de los parámetros—, así que un VIEWER con la suscripción vencida recibía
402 («tu plan no habilita») en vez de 403 («no tenés permiso»). El orden es
403 (rol) → 402 (suscripción) → 428 (PIN).

Las rutas se enumeran por introspección de `route.dependant`, como en
`test_subscription_write_gate.py`: una alta nueva con gate y rol queda cubierta
sola, sin acordarse de este archivo.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from functools import cache
from typing import Any

import pytest
import pytest_asyncio
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import deps
from app.main import create_app
from app.persistence.db.base import Base
from app.persistence.models.tenant import Subscription, Tenant
from app.persistence.models.user import User
from app.tests.api.v1.test_customers import _person
from app.tests.api.v1.test_permisos_de_importacion import _headers_de
from app.tests.conftest import FakeRedis, _pin_window_key

#: Dependencias que deciden por ROL (403). Las fábricas (`require_role`,
#: `require_pos_permission`) devuelven un cierre nuevo por llamada: se las
#: reconoce por el nombre calificado.
_DEPS_DE_ROL: frozenset[Callable[..., Any]] = frozenset(
    {
        deps.require_modify_role,
        deps.require_modify_access,
        deps.require_capital_role,
        deps.require_capital_write,
        deps.require_owner_stepup,
        deps.require_void_access,
        deps.require_discrepancy_view,
        deps.require_discrepancy_access,
    }
)
_FABRICAS_DE_ROL = ("require_role.<locals>.", "require_pos_permission.<locals>.")

#: Las que además piden la ventana de PIN (428).
_DEPS_CON_PIN: frozenset[Callable[..., Any]] = frozenset(
    {
        deps.require_modify_access,
        deps.require_capital_write,
        deps.require_owner_stepup,
        deps.require_void_access,
        deps.require_discrepancy_access,
    }
)


def _es_de_rol(call: Any) -> bool:
    if call in _DEPS_DE_ROL:
        return True
    return getattr(call, "__qualname__", "").startswith(_FABRICAS_DE_ROL)


def _contiene(dep: Dependant, criterio: Callable[[Any], bool]) -> bool:
    pendientes = [dep]
    while pendientes:
        actual = pendientes.pop()
        if criterio(actual.call):
            return True
        pendientes.extend(actual.dependencies)
    return False


def _es_gate(call: Any) -> bool:
    return call is deps.require_active_subscription


@cache
def _rutas_con_gate_y_rol() -> tuple[tuple[str, str, bool], ...]:
    """(método, ruta, exige PIN) de cada alta con gate de suscripción y rol."""
    salida: list[tuple[str, str, bool]] = []
    for route in create_app().routes:
        if not isinstance(route, APIRoute):
            continue
        if not _contiene(route.dependant, _es_gate) or not _contiene(
            route.dependant, _es_de_rol
        ):
            continue
        pide_pin = _contiene(route.dependant, lambda c: c in _DEPS_CON_PIN)
        for metodo in sorted(route.methods):
            salida.append((metodo, route.path, pide_pin))
    return tuple(sorted(salida))


def _por_ruta() -> dict[tuple[str, str], APIRoute]:
    return {
        (m, r.path): r
        for r in create_app().routes
        if isinstance(r, APIRoute)
        for m in r.methods
    }


_RUTAS = _rutas_con_gate_y_rol()

P = "/api/v1"

#: Las altas con gate que además piden PIN. Explícito a propósito: si se
#: dedujera de las dependencias, sacarle el PIN a una ruta la sacaría también
#: de acá y el 428 dejaría de probarse en silencio.
RUTAS_CON_PIN: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", f"{P}/capital-movements"),
        ("POST", f"{P}/expenses/profit-withdrawal"),
        ("POST", f"{P}/products/{{product_id}}/opening"),
        ("POST", f"{P}/products/batch"),
        ("POST", f"{P}/customers/import/confirm"),
    }
)


def _id(ruta: tuple[str, str, bool]) -> str:
    return f"{ruta[0]} {ruta[1]}"


# ── Estructural ───────────────────────────────────────────────────────────────


def test_la_enumeracion_no_es_vacia() -> None:
    """Sin esto, un cambio en la introspección dejaría pasar todo en vacío."""
    rutas = {(m, p) for m, p, _ in _RUTAS}
    assert ("POST", f"{P}/customers") in rutas
    assert ("POST", f"{P}/others/bulk-import") in rutas
    assert rutas >= RUTAS_CON_PIN


def test_las_altas_con_pin_son_exactamente_estas() -> None:
    """Sacarle el PIN a una alta, o sumar una nueva que lo pida, obliga a tocar
    `RUTAS_CON_PIN`."""
    assert {(m, p) for m, p, pide_pin in _RUTAS if pide_pin} == RUTAS_CON_PIN


def test_el_rol_se_resuelve_antes_que_el_gate() -> None:
    """FastAPI resuelve `route.dependant.dependencies` en orden (primero las del
    decorador): la primera que decide por rol tiene que ir antes que el gate."""
    por_ruta = _por_ruta()
    mal: list[str] = []
    for metodo, ruta, _ in _RUTAS:
        tope = por_ruta[(metodo, ruta)].dependant.dependencies
        rol = next(i for i, d in enumerate(tope) if _contiene(d, _es_de_rol))
        gate = next(i for i, d in enumerate(tope) if _contiene(d, _es_gate))
        if rol > gate:
            mal.append(f"{metodo} {ruta}")
    assert not mal, f"el gate de suscripción corre antes que el rol en: {mal}"


# ── Por HTTP ──────────────────────────────────────────────────────────────────


def _url(ruta: str) -> str:
    """Path params UUID al azar: las dependencias corren antes de buscar nada."""
    partes = ruta.split("/")
    return "/".join(
        str(uuid.uuid4()) if p.startswith("{") and p.endswith("}") else p for p in partes
    )


async def _vencer(db: AsyncSession, tenant: Tenant) -> None:
    await db.execute(
        update(Subscription)
        .where(Subscription.tenant_id == tenant.tenant_id)
        .values(plan_code="control", status="READ_ONLY", seats_included=3)
    )
    await db.commit()


#: Telemetría de uso (F5): se escribe en `get_current_user`, antes de que la ruta
#: rechace. En producción el rechazo hace rollback de la sesión y la fila no queda;
#: este fixture comparte la sesión y no la revierte, así que se ve acá. No es
#: estado del negocio.
_TABLAS_DE_TELEMETRIA = frozenset({"tenant_daily_activity"})


async def _conteos(db: AsyncSession) -> dict[str, int]:
    """Filas de cada tabla: un rechazo por dependencia no escribe NADA."""
    return {
        t.name: int((await db.execute(select(func.count()).select_from(t))).scalar_one())
        for t in Base.metadata.sorted_tables
        if t.name not in _TABLAS_DE_TELEMETRIA
    }


@pytest_asyncio.fixture(params=["VIEWER", "ANALYST"])
async def sin_permiso(
    request: pytest.FixtureRequest, db_session: AsyncSession, sample_tenant: Tenant
) -> dict[str, str]:
    return await _headers_de(db_session, sample_tenant, str(request.param))


@pytest.mark.parametrize("ruta", _RUTAS, ids=_id)
async def test_sin_permiso_y_vencida_da_403(
    ruta: tuple[str, str, bool],
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sin_permiso: dict[str, str],
) -> None:
    await _vencer(db_session, sample_tenant)
    antes = await _conteos(db_session)

    resp = await client.request(ruta[0], _url(ruta[1]), json={}, headers=sin_permiso)

    assert resp.status_code == 403, f"{_id(ruta)}: {resp.status_code} {resp.text}"
    assert await _conteos(db_session) == antes


@pytest.mark.parametrize("ruta", _RUTAS, ids=_id)
async def test_autorizado_y_vencida_da_402_antes_que_el_pin(
    ruta: tuple[str, str, bool],
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sample_user: User,
    auth_headers: dict[str, str],
    fake_redis: FakeRedis,
) -> None:
    """Sin ventana de PIN a propósito: el 402 tiene que salir antes del 428."""
    await fake_redis.delete(_pin_window_key(sample_tenant.tenant_id, sample_user.user_id))
    await _vencer(db_session, sample_tenant)
    antes = await _conteos(db_session)

    resp = await client.request(ruta[0], _url(ruta[1]), json={}, headers=auth_headers)

    assert resp.status_code == 402, f"{_id(ruta)}: {resp.status_code} {resp.text}"
    assert resp.json()["detail"]["code"] == "SUBSCRIPTION_READ_ONLY"
    assert await _conteos(db_session) == antes


@pytest.mark.parametrize("ruta", sorted(RUTAS_CON_PIN), ids=lambda r: f"{r[0]} {r[1]}")
async def test_autorizado_activa_y_sin_pin_da_428(
    ruta: tuple[str, str],
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sample_user: User,
    auth_headers: dict[str, str],
    fake_redis: FakeRedis,
) -> None:
    """Mover el rol al decorador no le saca el PIN a ninguna ruta."""
    await fake_redis.delete(_pin_window_key(sample_tenant.tenant_id, sample_user.user_id))
    antes = await _conteos(db_session)

    resp = await client.request(ruta[0], _url(ruta[1]), json={}, headers=auth_headers)

    assert resp.status_code == 428, f"{ruta}: {resp.status_code} {resp.text}"
    assert await _conteos(db_session) == antes


#: Las dependencias que piden «OWNER o `can_modify_sensitive`» (más OWNER/ADMIN
#: en las de capital): un ADMIN sin el permiso no pasa, a diferencia de
#: `require_role("OWNER", "ADMIN")`.
_DEPS_DE_MODIFICAR: frozenset[Callable[..., Any]] = frozenset(
    {
        deps.require_modify_role,
        deps.require_modify_access,
        deps.require_capital_role,
        deps.require_capital_write,
    }
)
_RUTAS_DE_MODIFICAR = tuple(
    sorted(
        (m, p)
        for (m, p), r in _por_ruta().items()
        if (m, p) in {(m2, p2) for m2, p2, _ in _RUTAS}
        and _contiene(r.dependant, lambda c: c in _DEPS_DE_MODIFICAR)
    )
)


def test_las_rutas_de_modificar_estan_enumeradas() -> None:
    assert set(_RUTAS_DE_MODIFICAR) >= RUTAS_CON_PIN


@pytest_asyncio.fixture
async def admin_sin_permiso(
    db_session: AsyncSession, sample_tenant: Tenant
) -> dict[str, str]:
    """ADMIN con `can_modify_sensitive=False` (el default)."""
    return await _headers_de(db_session, sample_tenant, "ADMIN")


@pytest.mark.parametrize("ruta", _RUTAS_DE_MODIFICAR, ids=lambda r: f"{r[0]} {r[1]}")
async def test_admin_sin_permiso_de_modificar_y_vencida_da_403(
    ruta: tuple[str, str],
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    admin_sin_permiso: dict[str, str],
) -> None:
    """El rol de estas rutas no es `require_role("OWNER", "ADMIN")`: un ADMIN sin
    `can_modify_sensitive` frena en el 403, antes que la suscripción."""
    await _vencer(db_session, sample_tenant)
    antes = await _conteos(db_session)

    resp = await client.request(ruta[0], _url(ruta[1]), json={}, headers=admin_sin_permiso)

    assert resp.status_code == 403, f"{ruta}: {resp.status_code} {resp.text}"
    assert await _conteos(db_session) == antes


async def test_el_mismo_admin_pasa_el_rol_de_un_alta_comun_y_frena_en_402(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    admin_sin_permiso: dict[str, str],
) -> None:
    """Contracara: en `POST /customers` (`require_role("OWNER", "ADMIN")`) el
    mismo ADMIN pasa el rol y lo frena la suscripción."""
    await _vencer(db_session, sample_tenant)
    antes = await _conteos(db_session)

    resp = await client.post(f"{P}/customers", json={}, headers=admin_sin_permiso)

    assert resp.status_code == 402, resp.text
    assert resp.json()["detail"]["code"] == "SUBSCRIPTION_READ_ONLY"
    assert await _conteos(db_session) == antes


async def test_el_dueno_con_la_suscripcion_activa_sigue_dando_de_alta(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    auth_headers: dict[str, str],
) -> None:
    """El rol pasó del parámetro al decorador: el autorizado sigue escribiendo."""
    from app.persistence.models.customer import Customer  # noqa: PLC0415

    def _clientes() -> Any:
        return select(func.count(Customer.id)).where(
            Customer.tenant_id == sample_tenant.tenant_id
        )

    antes = int((await db_session.execute(_clientes())).scalar_one())

    resp = await client.post(
        "/api/v1/customers", json=_person("Cliente R7"), headers=auth_headers
    )

    assert resp.status_code == 201, resp.text
    assert int((await db_session.execute(_clientes())).scalar_one()) == antes + 1
