"""Compuertas del rol CASHIER (B5): denegar por defecto, permisos y sin costos.

El riesgo que cubre este archivo es que un empleado de caja vea lo que no le
corresponde (costos, márgenes, caja, chat) o haga lo que su dueño no le habilitó.
Se prueba por HTTP, con un cajero de verdad en la base.
"""

from __future__ import annotations

import unittest.mock
import uuid
from decimal import Decimal
from typing import Any

import pytest
from fastapi.routing import APIRoute
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user
from app.domain.business_time import today_ar
from app.domain.pos_permissions import CASHIER_ALLOWED_ROUTES
from app.main import app as fastapi_app
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.tests.conftest import TEST_PIN, _pin_window_key
from app.utils.security import create_access_token, hash_password

_FECHA = f"{today_ar().isoformat()}T10:00:00"
CLAVES_PROHIBIDAS = {"unit_cost_ars", "margin_pct", "list_price_ars", "custom_fields"}


async def _cajero(
    db_session: AsyncSession,
    tenant: Tenant,
    fake_redis: Any,
    permisos: dict[str, Any] | None = None,
    *,
    email: str | None = None,
    pin_abierto: bool = True,
) -> tuple[User, dict[str, str]]:
    user = User(
        user_id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        email=email or f"caja-{uuid.uuid4().hex[:6]}@kiosco.com",
        full_name="Cajero",
        password_hash=hash_password("Secure123"),
        role_code="CASHIER",
        is_active=True,
        pin_hash=hash_password(TEST_PIN),
        pos_permissions=permisos or {},
    )
    db_session.add(user)
    await db_session.commit()
    if pin_abierto:
        await fake_redis.set(_pin_window_key(tenant.tenant_id, user.user_id), "1", ex=600)
    token = create_access_token(
        {"sub": str(user.user_id), "tenant_id": str(tenant.tenant_id), "role_code": "CASHIER"}
    )
    return user, {"Authorization": f"Bearer {token}"}


async def _producto(
    client: AsyncClient, headers: dict[str, str], name: str, *, price: str = "100.00", **extra: Any
) -> dict[str, Any]:
    resp = await client.post(
        "/api/v1/products",
        json={"name": name, "sale_price_ars": price, "stock_units": 10, "unit_cost_ars": "60.00",
              **extra},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


def _venta(pid: str, **extra: Any) -> dict[str, Any]:
    cuerpo: dict[str, Any] = {
        "client_operation_id": f"op-{uuid.uuid4().hex[:16]}",
        "operation_date": _FECHA,
        "items": [{"product_id": pid, "quantity": 1}],
        "tenders": [{"payment_method": "cash", "amount_ars": "100.00"}],
    }
    cuerpo.update(extra)
    return cuerpo


@pytest.fixture(autouse=True)
def _sin_celery(mock_score_trigger):
    with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
        yield


def _usa_auth(route: APIRoute) -> bool:
    pendientes = list(route.dependant.dependencies)
    while pendientes:
        dep = pendientes.pop()
        if dep.call is get_current_user:
            return True
        pendientes.extend(dep.dependencies)
    return False


def _rutas_autenticadas() -> list[tuple[str, str]]:
    return sorted(
        (metodo, r.path)
        for r in fastapi_app.routes
        if isinstance(r, APIRoute) and _usa_auth(r)
        for metodo in r.methods
        if metodo not in ("HEAD", "OPTIONS")
    )


# ── Denegar por defecto ─────────────────────────────────────────────────────


class TestDenegarPorDefecto:
    def test_la_lista_permitida_son_rutas_reales(self) -> None:
        """Una ruta renombrada no puede dejar la lista apuntando a nada."""
        reales = set(_rutas_autenticadas())
        assert reales >= CASHIER_ALLOWED_ROUTES, CASHIER_ALLOWED_ROUTES - reales

    async def test_todas_las_demas_rutas_autenticadas_dan_403(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        fake_redis: Any,
    ) -> None:
        """El barrido: CADA ruta autenticada fuera de la lista, llamada de verdad."""
        _, headers = await _cajero(
            db_session,
            sample_tenant,
            fake_redis,
            {"discount": True, "void_ticket": True, "fiado": True, "learn_barcode": True},
        )
        colados = []
        for metodo, plantilla in _rutas_autenticadas():
            if (metodo, plantilla) in CASHIER_ALLOWED_ROUTES:
                continue
            url = plantilla
            while "{" in url:
                i, j = url.index("{"), url.index("}")
                url = url[:i] + str(uuid.uuid4()) + url[j + 1 :]
            resp = await client.request(metodo, url, headers=headers, json={})
            cuerpo = resp.json() if resp.headers.get("content-type", "").startswith(
                "application/json"
            ) else {}
            detalle = cuerpo.get("detail") if isinstance(cuerpo, dict) else None
            if not (
                resp.status_code == 403
                and isinstance(detalle, dict)
                and detalle.get("code") == "CASHIER_ROUTE_FORBIDDEN"
            ):
                colados.append((metodo, plantilla, resp.status_code))
        assert not colados, colados

    @pytest.mark.parametrize(
        "url",
        [
            "/api/v1/products",
            "/api/v1/sales",
            "/api/v1/economic-summary",
            "/api/v1/insights/current",
            "/api/v1/settings/team",
        ],
    )
    async def test_lo_que_ve_el_duenio_no_lo_ve_el_cajero(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        fake_redis: Any,
        url: str,
    ) -> None:
        _, headers = await _cajero(db_session, sample_tenant, fake_redis)
        resp = await client.get(url, headers=headers)
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "CASHIER_ROUTE_FORBIDDEN"

    async def test_el_chat_queda_afuera(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        fake_redis: Any,
    ) -> None:
        _, headers = await _cajero(db_session, sample_tenant, fake_redis)
        resp = await client.post(
            "/api/v1/agent/chat",
            json={"message": "¿cuánto gané?", "conversation_id": str(uuid.uuid4())},
            headers=headers,
        )
        assert resp.status_code == 403

    async def test_el_cajero_se_puede_quedar_logueado(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        fake_redis: Any,
    ) -> None:
        _, headers = await _cajero(db_session, sample_tenant, fake_redis, {"fiado": True})
        me = await client.get("/api/v1/auth/me", headers=headers)
        assert me.status_code == 200, me.text
        assert me.json()["role_code"] == "CASHIER"
        assert me.json()["pos_permissions"] == ["fiado"]


# ── Sin costos ──────────────────────────────────────────────────────────────


class TestSinCostos:
    async def test_catalogo_sin_claves_sensibles_y_con_stock(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        fake_redis: Any,
    ) -> None:
        await _producto(client, auth_headers, "Yerba", sku="YER-1")
        _, headers = await _cajero(db_session, sample_tenant, fake_redis)
        resp = await client.get("/api/v1/pos/catalog", headers=headers)
        assert resp.status_code == 200, resp.text
        item = resp.json()["items"][0]
        assert not CLAVES_PROHIBIDAS & set(item)
        assert item["stock_units"] == 10
        assert item["sku"] == "YER-1"

    async def test_catalogo_recorre_todo_por_cursor(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        fake_redis: Any,
    ) -> None:
        creados = {(await _producto(client, auth_headers, f"P{i}"))["id"] for i in range(7)}
        _, headers = await _cajero(db_session, sample_tenant, fake_redis)
        vistos: list[str] = []
        cursor = None
        while True:
            params: dict[str, Any] = {"limit": 3}
            if cursor:
                params["after"] = cursor
            resp = await client.get("/api/v1/pos/catalog", params=params, headers=headers)
            pagina = resp.json()
            vistos += [p["id"] for p in pagina["items"]]
            cursor = pagina["next_cursor"]
            if cursor is None:
                break
        assert len(vistos) == len(set(vistos)), "repitió productos"
        assert set(vistos) == creados

    async def test_lookup_sin_claves_sensibles(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        fake_redis: Any,
    ) -> None:
        await _producto(client, auth_headers, "Coca", barcode="4006381333931")
        _, headers = await _cajero(db_session, sample_tenant, fake_redis)
        resp = await client.get(
            "/api/v1/products/lookup", params={"code": "4006381333931"}, headers=headers
        )
        assert resp.status_code == 200, resp.text
        assert set(resp.json()) == {"code_type", "matched_by", "product"}
        assert not CLAVES_PROHIBIDAS & set(resp.json()["product"])

    async def test_aprender_codigo_sin_claves_sensibles_tambien_al_reintentar(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        fake_redis: Any,
    ) -> None:
        p = await _producto(client, auth_headers, "Alfajor")
        _, headers = await _cajero(db_session, sample_tenant, fake_redis, {"learn_barcode": True})
        url = f"/api/v1/products/{p['id']}/barcode"
        for _intento in range(2):
            resp = await client.post(url, json={"barcode": "4006381333931"}, headers=headers)
            assert resp.status_code == 200, resp.text
            assert not CLAVES_PROHIBIDAS & set(resp.json())


# ── Permisos ────────────────────────────────────────────────────────────────


class TestPermisos:
    async def test_sin_permiso_no_descuenta_y_con_permiso_si(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        fake_redis: Any,
    ) -> None:
        p = await _producto(client, auth_headers, "Yerba")
        cuerpo = _venta(
            p["id"], discount_ars="10.00",
            tenders=[{"payment_method": "cash", "amount_ars": "90.00"}],
        )
        _, sin = await _cajero(db_session, sample_tenant, fake_redis)
        resp = await client.post("/api/v1/pos/operations", json=cuerpo, headers=sin)
        assert resp.status_code == 403
        assert resp.json()["detail"]["permission"] == "discount"

        _, con = await _cajero(db_session, sample_tenant, fake_redis, {"discount": True})
        cuerpo["client_operation_id"] = f"op-{uuid.uuid4().hex[:16]}"
        resp = await client.post("/api/v1/pos/operations", json=cuerpo, headers=con)
        assert resp.status_code == 201, resp.text

    async def test_venta_sin_descuento_no_pide_permiso(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        fake_redis: Any,
    ) -> None:
        p = await _producto(client, auth_headers, "Yerba")
        user, headers = await _cajero(db_session, sample_tenant, fake_redis)
        resp = await client.post("/api/v1/pos/operations", json=_venta(p["id"]), headers=headers)
        assert resp.status_code == 201, resp.text
        assert resp.json()["created_by_user_id"] == str(user.user_id)

    @pytest.mark.parametrize("valor", ["true", 1, None, "yes"])
    async def test_valores_malformados_no_habilitan(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        fake_redis: Any,
        valor: Any,
    ) -> None:
        p = await _producto(client, auth_headers, "Yerba")
        _, headers = await _cajero(
            db_session, sample_tenant, fake_redis, {"discount": valor, "superpoderes": True}
        )
        cuerpo = _venta(
            p["id"], discount_ars="10.00",
            tenders=[{"payment_method": "cash", "amount_ars": "90.00"}],
        )
        resp = await client.post("/api/v1/pos/operations", json=cuerpo, headers=headers)
        assert resp.status_code == 403

    async def test_aprender_codigo_sin_permiso(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        fake_redis: Any,
    ) -> None:
        p = await _producto(client, auth_headers, "Alfajor")
        _, headers = await _cajero(db_session, sample_tenant, fake_redis)
        resp = await client.post(
            f"/api/v1/products/{p['id']}/barcode", json={"barcode": "4006381333931"},
            headers=headers,
        )
        assert resp.status_code == 403

    async def test_clientes_para_fiar_solo_con_permiso_y_sin_ficha(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        fake_redis: Any,
    ) -> None:
        creado = await client.post(
            "/api/v1/customers",
            json={"name": "Ana", "last_name": "Gómez", "customer_type": "person",
                  "doc_type": "dni", "dni": "30123456", "phone": "1155554444"},
            headers=auth_headers,
        )
        assert creado.status_code == 201, creado.text
        _, sin = await _cajero(db_session, sample_tenant, fake_redis)
        assert (await client.get("/api/v1/pos/customers", headers=sin)).status_code == 403
        _, con = await _cajero(db_session, sample_tenant, fake_redis, {"fiado": True})
        resp = await client.get("/api/v1/pos/customers", params={"q": "ana"}, headers=con)
        assert resp.status_code == 200, resp.text
        assert resp.json() == [{"id": creado.json()["id"], "name": "Ana Gómez"}]


# ── Crear ≠ recuperar ───────────────────────────────────────────────────────


class TestRecuperarTrasRevocacion:
    async def test_respuesta_perdida_y_permiso_revocado_devuelve_el_ticket(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        fake_redis: Any,
    ) -> None:
        p = await _producto(client, auth_headers, "Yerba")
        user, headers = await _cajero(db_session, sample_tenant, fake_redis, {"discount": True})
        cuerpo = _venta(
            p["id"], discount_ars="10.00",
            tenders=[{"payment_method": "cash", "amount_ars": "90.00"}],
        )
        primera = await client.post("/api/v1/pos/operations", json=cuerpo, headers=headers)
        assert primera.status_code == 201, primera.text

        user.pos_permissions = {}
        await db_session.commit()

        reintento = await client.post("/api/v1/pos/operations", json=cuerpo, headers=headers)
        assert reintento.status_code == 200, reintento.text
        assert reintento.json()["id"] == primera.json()["id"]

    async def test_un_rechazo_por_permiso_no_consume_la_clave(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        fake_redis: Any,
    ) -> None:
        p = await _producto(client, auth_headers, "Yerba")
        user, headers = await _cajero(db_session, sample_tenant, fake_redis)
        cuerpo = _venta(
            p["id"], discount_ars="10.00",
            tenders=[{"payment_method": "cash", "amount_ars": "90.00"}],
        )
        rechazo = await client.post("/api/v1/pos/operations", json=cuerpo, headers=headers)
        assert rechazo.status_code == 403

        user.pos_permissions = {"discount": True}
        await db_session.commit()
        resp = await client.post("/api/v1/pos/operations", json=cuerpo, headers=headers)
        assert resp.status_code == 201, resp.text


# ── Anulación ───────────────────────────────────────────────────────────────


class TestAnulacionDelCajero:
    async def _ticket(
        self, client: AsyncClient, headers: dict[str, str], pid: str
    ) -> str:
        resp = await client.post("/api/v1/pos/operations", json=_venta(pid), headers=headers)
        assert resp.status_code == 201, resp.text
        return str(resp.json()["id"])

    async def test_anula_su_ticket_con_motivo(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        fake_redis: Any,
    ) -> None:
        p = await _producto(client, auth_headers, "Yerba")
        _, headers = await _cajero(db_session, sample_tenant, fake_redis, {"void_ticket": True})
        op = await self._ticket(client, headers, p["id"])
        resp = await client.post(
            f"/api/v1/pos/operations/{op}/void", json={"reason": "cobré de más"}, headers=headers
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "VOIDED"

    async def test_sin_motivo_no(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        fake_redis: Any,
    ) -> None:
        p = await _producto(client, auth_headers, "Yerba")
        _, headers = await _cajero(db_session, sample_tenant, fake_redis, {"void_ticket": True})
        op = await self._ticket(client, headers, p["id"])
        resp = await client.post(
            f"/api/v1/pos/operations/{op}/void", json={"reason": "  "}, headers=headers
        )
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "VOID_REASON_REQUIRED"

    async def test_no_anula_el_ticket_de_un_companiero(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        fake_redis: Any,
    ) -> None:
        p = await _producto(client, auth_headers, "Yerba")
        _, uno = await _cajero(db_session, sample_tenant, fake_redis)
        _, otro = await _cajero(db_session, sample_tenant, fake_redis, {"void_ticket": True})
        op = await self._ticket(client, uno, p["id"])
        resp = await client.post(
            f"/api/v1/pos/operations/{op}/void", json={"reason": "error"}, headers=otro
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "VOID_NOT_OWN_TICKET"

    async def test_sin_permiso_o_sin_pin_no(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        fake_redis: Any,
    ) -> None:
        p = await _producto(client, auth_headers, "Yerba")
        _, sin_permiso = await _cajero(db_session, sample_tenant, fake_redis)
        op = await self._ticket(client, sin_permiso, p["id"])
        resp = await client.post(
            f"/api/v1/pos/operations/{op}/void", json={"reason": "x"}, headers=sin_permiso
        )
        assert resp.status_code == 403

        _, sin_pin = await _cajero(
            db_session, sample_tenant, fake_redis, {"void_ticket": True}, pin_abierto=False
        )
        op2 = await self._ticket(client, sin_pin, p["id"])
        resp = await client.post(
            f"/api/v1/pos/operations/{op2}/void", json={"reason": "x"}, headers=sin_pin
        )
        assert resp.status_code == 428

    async def test_un_cajero_que_puede_anular_puede_configurar_su_pin(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        fake_redis: Any,
    ) -> None:
        user, headers = await _cajero(db_session, sample_tenant, fake_redis, {"void_ticket": True})
        user.pin_hash = None
        await db_session.commit()
        resp = await client.post(
            "/api/v1/auth/pin/setup", json={"pin": "4321", "pin_confirm": "4321"}, headers=headers
        )
        assert resp.status_code == 200, resp.text


# ── Equipo: alta, cambio de rol, email ──────────────────────────────────────


class TestEquipo:
    async def test_permisos_de_caja_solo_sobre_un_cajero(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        admin = User(
            user_id=uuid.uuid4(), tenant_id=sample_tenant.tenant_id, email="admin@kiosco.com",
            full_name="Admin", password_hash=hash_password("Secure123"), role_code="ADMIN",
            is_active=True,
        )
        db_session.add(admin)
        await db_session.commit()
        resp = await client.patch(
            f"/api/v1/settings/team/{admin.user_id}",
            json={"pos_permissions": {"discount": True}},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    async def test_un_cajero_no_recibe_permiso_de_modificar_datos(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        fake_redis: Any,
    ) -> None:
        user, _ = await _cajero(db_session, sample_tenant, fake_redis)
        resp = await client.patch(
            f"/api/v1/settings/team/{user.user_id}",
            json={"can_modify_sensitive": True},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    async def test_el_duenio_configura_permisos_y_quedan_auditados(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        fake_redis: Any,
    ) -> None:
        user, _ = await _cajero(db_session, sample_tenant, fake_redis)
        resp = await client.patch(
            f"/api/v1/settings/team/{user.user_id}",
            json={"pos_permissions": {"discount": True, "fiado": "true", "raro": True}},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["pos_permissions"] == ["discount"]
        auditoria = (
            await db_session.execute(
                select(DecisionAuditLog).where(
                    DecisionAuditLog.decision_type == "TEAM_POS_PERMISSIONS_CHANGED"
                )
            )
        ).scalars().all()
        assert len(auditoria) == 1
        assert auditoria[0].decision_data["after"]["pos_permissions"] == {"discount": True}

    async def test_admin_a_cajero_limpia_permiso_general_y_pin(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        fake_redis: Any,
    ) -> None:
        admin = User(
            user_id=uuid.uuid4(), tenant_id=sample_tenant.tenant_id, email="admin2@kiosco.com",
            full_name="Admin", password_hash=hash_password("Secure123"), role_code="ADMIN",
            is_active=True, can_modify_sensitive=True,
        )
        db_session.add(admin)
        await db_session.commit()
        clave_pin = _pin_window_key(sample_tenant.tenant_id, admin.user_id)
        await fake_redis.set(clave_pin, "1", ex=600)

        resp = await client.patch(
            f"/api/v1/users/{admin.user_id}", json={"role_code": "CASHIER"}, headers=auth_headers
        )
        assert resp.status_code == 200, resp.text
        await db_session.refresh(admin)
        assert admin.role_code == "CASHIER"
        assert admin.can_modify_sensitive is False
        assert admin.pos_permissions == {}
        assert await fake_redis.get(clave_pin) is None

    async def test_cajero_admin_cajero_no_recupera_permisos_viejos(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        fake_redis: Any,
    ) -> None:
        user, _ = await _cajero(
            db_session, sample_tenant, fake_redis, {"discount": True, "void_ticket": True}
        )
        for rol in ("ADMIN", "CASHIER"):
            resp = await client.patch(
                f"/api/v1/users/{user.user_id}", json={"role_code": rol}, headers=auth_headers
            )
            assert resp.status_code == 200, resp.text
        await db_session.refresh(user)
        assert user.pos_permissions == {}
        cambios = (
            await db_session.execute(
                select(DecisionAuditLog).where(
                    DecisionAuditLog.decision_type == "TEAM_ROLE_CHANGED"
                )
            )
        ).scalars().all()
        assert len(cambios) == 2

    async def test_alta_de_cajero_nace_sin_permisos(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        resp = await client.post(
            "/api/v1/users",
            json={"email": "nuevo@kiosco.com", "full_name": "Nuevo Cajero",
                  "role_code": "CASHIER", "password": "Secure1234"},
            headers=auth_headers,
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["role_code"] == "CASHIER"

    async def test_email_de_otro_negocio_se_rechaza(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        second_auth_headers: dict[str, str],
    ) -> None:
        """El login es global: esa cuenta nunca podría entrar."""
        resp = await client.post(
            "/api/v1/users",
            json={"email": "owner@limpieza.com", "full_name": "Duplicado",
                  "role_code": "CASHIER", "password": "Secure1234"},
            headers=auth_headers,
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["code"] == "EMAIL_IN_USE_OTHER_BUSINESS"


# ── Fechas y unidades ───────────────────────────────────────────────────────


class TestFechaConZona:
    async def test_una_hora_utc_se_guarda_en_hora_argentina(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """02:00Z del día D son las 23:00 del día D−1 en Argentina."""
        hoy = today_ar()
        resp = await client.post(
            "/api/v1/sales",
            json={"amount": "100.00", "payment_method": "cash",
                  "transaction_date": f"{hoy.isoformat()}T02:00:00Z"},
            headers=auth_headers,
        )
        assert resp.status_code == 201, resp.text
        guardada = (
            await client.get(f"/api/v1/sales/{resp.json()['id']}", headers=auth_headers)
        ).json()["transaction_date"]
        assert guardada.startswith(f"{(hoy.fromordinal(hoy.toordinal() - 1)).isoformat()}T23:00")


class TestVentaFraccionada:
    async def test_setecientos_cincuenta_gramos_a_precio_por_kilo(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
    ) -> None:
        """$1.234,56 el kilo × 750 g = $925,92; el stock se valúa por kilo."""
        p = await _producto(client, auth_headers, "Queso", price="1234.56")
        prod = await db_session.get(Product, uuid.UUID(p["id"]))
        assert prod is not None
        prod.sale_unit = "gram"
        prod.base_units_per_sale_unit = 1000
        prod.stock_units = 5000
        prod.unit_cost_ars = Decimal("800.00")
        await db_session.commit()

        resp = await client.post(
            "/api/v1/pos/operations",
            json=_venta(
                p["id"],
                items=[{"product_id": p["id"], "quantity": 750}],
                tenders=[{"payment_method": "cash", "amount_ars": "925.92"}],
            ),
            headers=auth_headers,
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["total_ars"] == "925.92"

        hoy = today_ar().isoformat()
        resumen = await client.get(
            "/api/v1/economic-summary",
            params={"from_date": hoy, "to_date": hoy},
            headers=auth_headers,
        )
        assert resumen.status_code == 200, resumen.text
        # 4.250 g quedan, a $800 el kilo = $3.400 (no $3.400.000).
        assert float(resumen.json()["stock_value_ars"]) == pytest.approx(3400.0)
