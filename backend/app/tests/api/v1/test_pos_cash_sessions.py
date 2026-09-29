"""Turno de caja y su arqueo (B8 del POS).

Lo que se protege acá, en orden de importancia:

1. **Un arqueo cerrado no se reescribe.** Es el invariante del bloque. Una venta
   que sincroniza después del cierre —el caso normal de una caja que estuvo sin
   red— no cambia el número que el cajero firmó: aparece en la conciliación.
2. **El turno se adopta**, porque la caja lo abre sin red y avisa después.
3. **Cada cajero cierra el suyo**, con su propio conteo.
"""

import unittest.mock
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.business_time import today_ar
from app.domain.pos_terminal import TERMINAL_HEADER
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.pos_cash_session import PosCashSession
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.tests.conftest import TEST_PIN, _pin_window_key
from app.utils.security import create_access_token, hash_password

_FECHA = f"{today_ar().isoformat()}T10:00:00"


@pytest.fixture(autouse=True)
def _sin_celery(mock_score_trigger):
    with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
        yield


async def _caja(client: AsyncClient, headers: dict[str, str]) -> dict[str, str]:
    alta = await client.post(
        "/api/v1/pos/terminals",
        json={"name": f"Caja {uuid.uuid4().hex[:4]}"},
        headers=headers,
    )
    assert alta.status_code == 201, alta.text
    return {**headers, TERMINAL_HEADER: alta.json()["secret"]}


async def _producto(client: AsyncClient, headers: dict[str, str], *, stock: int = 100) -> str:
    resp = await client.post(
        "/api/v1/products",
        json={
            "name": f"Yerba {uuid.uuid4().hex[:5]}",
            "sale_price_ars": "100.00",
            "stock_units": stock,
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def _venta(pid: str, turno: str, *, cantidad: int = 1, metodo: str = "cash") -> dict[str, Any]:
    return {
        "client_operation_id": f"op-{uuid.uuid4().hex[:16]}",
        "operation_date": _FECHA,
        "client_session_id": turno,
        "items": [{"product_id": pid, "quantity": cantidad, "discount_ars": "0.00"}],
        "tenders": [{"payment_method": metodo, "amount_ars": f"{cantidad * 100}.00"}],
    }


async def _cobrar(
    client: AsyncClient, caja: dict[str, str], pid: str, turno: str, **kw: Any
) -> dict[str, Any]:
    resp = await client.post("/api/v1/pos/operations", json=_venta(pid, turno, **kw), headers=caja)
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestAdopcion:
    """La caja abre el turno sin red y el servidor lo adopta."""

    async def test_una_venta_de_un_turno_que_no_existe_lo_crea(
        self, client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
    ) -> None:
        # Exigir que el turno existiera antes rompería el arranque sin conexión
        # de B7b: la caja abre el turno con la PC sin red y avisa cuando vuelve.
        caja = await _caja(client, auth_headers)
        pid = await _producto(client, auth_headers)
        await _cobrar(client, caja, pid, "turno-sin-red")

        fila = (
            await db_session.execute(
                select(PosCashSession).where(
                    PosCashSession.client_session_id == "turno-sin-red"
                )
            )
        ).scalar_one()
        assert fila.status == "OPEN"

    async def test_abrir_despues_devuelve_el_mismo_turno(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """La caja avisa cuando vuelve la red: no puede crear un segundo turno."""
        caja = await _caja(client, auth_headers)
        pid = await _producto(client, auth_headers)
        await _cobrar(client, caja, pid, "turno-1")

        resp = await client.post(
            "/api/v1/pos/cash-sessions",
            json={"client_session_id": "turno-1", "opening_float_ars": "5000.00"},
            headers=caja,
        )
        assert resp.status_code == 201, resp.text
        listado = await client.get("/api/v1/pos/cash-sessions", headers=auth_headers)
        assert len([s for s in listado.json() if s["client_session_id"] == "turno-1"]) == 1

    async def test_abrir_dos_veces_es_idempotente(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        caja = await _caja(client, auth_headers)
        cuerpo = {"client_session_id": "turno-1", "opening_float_ars": "5000.00"}
        uno = await client.post("/api/v1/pos/cash-sessions", json=cuerpo, headers=caja)
        dos = await client.post("/api/v1/pos/cash-sessions", json=cuerpo, headers=caja)
        assert uno.status_code == 201 and dos.status_code == 201
        assert uno.json()["id"] == dos.json()["id"]


class TestArqueo:
    async def test_el_esperado_es_el_fondo_mas_el_efectivo(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        caja = await _caja(client, auth_headers)
        pid = await _producto(client, auth_headers)
        abrir = await client.post(
            "/api/v1/pos/cash-sessions",
            json={"client_session_id": "t1", "opening_float_ars": "5000.00"},
            headers=caja,
        )
        sid = abrir.json()["id"]
        await _cobrar(client, caja, pid, "t1", cantidad=3)  # 300 en efectivo

        cierre = await client.post(
            f"/api/v1/pos/cash-sessions/{sid}/close",
            json={"counted_cash_ars": "5300.00"},
            headers=caja,
        )
        assert cierre.status_code == 200, cierre.text
        assert cierre.json()["expected_cash_ars"] == "5300.00"
        assert cierre.json()["difference_ars"] == "0.00"

    async def test_lo_que_no_es_efectivo_no_entra_al_cajon(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """Una transferencia no está en el cajón: contarla daría un faltante falso."""
        caja = await _caja(client, auth_headers)
        pid = await _producto(client, auth_headers)
        abrir = await client.post(
            "/api/v1/pos/cash-sessions",
            json={"client_session_id": "t1", "opening_float_ars": "0.00"},
            headers=caja,
        )
        await _cobrar(client, caja, pid, "t1", cantidad=2, metodo="cash")
        await _cobrar(client, caja, pid, "t1", cantidad=5, metodo="transfer")

        cierre = await client.post(
            f"/api/v1/pos/cash-sessions/{abrir.json()['id']}/close",
            json={"counted_cash_ars": "200.00"},
            headers=caja,
        )
        assert cierre.json()["expected_cash_ars"] == "200.00"
        assert cierre.json()["difference_ars"] == "0.00"

    async def test_la_diferencia_se_informa_no_se_corrige(
        self, client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
    ) -> None:
        """Faltan $100: se registra y se audita. Nadie ajusta nada solo."""
        caja = await _caja(client, auth_headers)
        pid = await _producto(client, auth_headers)
        abrir = await client.post(
            "/api/v1/pos/cash-sessions",
            json={"client_session_id": "t1", "opening_float_ars": "0.00"},
            headers=caja,
        )
        await _cobrar(client, caja, pid, "t1", cantidad=5)

        cierre = await client.post(
            f"/api/v1/pos/cash-sessions/{abrir.json()['id']}/close",
            json={"counted_cash_ars": "400.00", "notes": "Faltan cien."},
            headers=caja,
        )
        assert cierre.json()["difference_ars"] == "-100.00"
        log = (
            await db_session.execute(
                select(DecisionAuditLog).where(
                    DecisionAuditLog.decision_type == "POS_CASH_SESSION_CLOSED"
                )
            )
        ).scalar_one()
        assert log.decision_data["difference_ars"] == "-100.00"
        assert log.decision_data["notes"] == "Faltan cien."

    async def test_cerrar_dos_veces_no_pisa_el_numero_firmado(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """No es un reintento inocente: el segundo cierre trae OTRO conteo."""
        caja = await _caja(client, auth_headers)
        abrir = await client.post(
            "/api/v1/pos/cash-sessions",
            json={"client_session_id": "t1", "opening_float_ars": "1000.00"},
            headers=caja,
        )
        sid = abrir.json()["id"]
        uno = await client.post(
            f"/api/v1/pos/cash-sessions/{sid}/close",
            json={"counted_cash_ars": "1000.00"},
            headers=caja,
        )
        assert uno.status_code == 200
        dos = await client.post(
            f"/api/v1/pos/cash-sessions/{sid}/close",
            json={"counted_cash_ars": "999999.00"},
            headers=caja,
        )
        assert dos.status_code == 409
        assert dos.json()["detail"]["code"] == "CASH_SESSION_ALREADY_CLOSED"

        detalle = await client.get(f"/api/v1/pos/cash-sessions/{sid}", headers=auth_headers)
        assert detalle.json()["counted_cash_ars"] == "1000.00"


class TestVentaTardia:
    """El invariante del bloque: un arqueo firmado no lo mueve nada."""

    async def test_una_venta_que_llega_tras_el_cierre_no_cambia_el_arqueo(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        caja = await _caja(client, auth_headers)
        pid = await _producto(client, auth_headers)
        abrir = await client.post(
            "/api/v1/pos/cash-sessions",
            json={"client_session_id": "t1", "opening_float_ars": "0.00"},
            headers=caja,
        )
        sid = abrir.json()["id"]
        await _cobrar(client, caja, pid, "t1", cantidad=2)
        cierre = await client.post(
            f"/api/v1/pos/cash-sessions/{sid}/close",
            json={"counted_cash_ars": "200.00"},
            headers=caja,
        )
        assert cierre.json()["expected_cash_ars"] == "200.00"

        # La caja estuvo sin red y sube ahora una venta de ese turno.
        await _cobrar(client, caja, pid, "t1", cantidad=7)

        detalle = await client.get(f"/api/v1/pos/cash-sessions/{sid}", headers=auth_headers)
        # El número firmado NO se movió.
        assert detalle.json()["expected_cash_ars"] == "200.00"
        assert detalle.json()["counted_cash_ars"] == "200.00"
        assert detalle.json()["difference_ars"] == "0.00"
        # Y la venta tardía aparece para conciliar, con lo que habría movido.
        assert len(detalle.json()["late_sales"]) == 1
        assert detalle.json()["late_sales"][0]["cash_ars"] == "700.00"

    async def test_el_turno_cerrado_no_se_reabre(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        caja = await _caja(client, auth_headers)
        pid = await _producto(client, auth_headers)
        abrir = await client.post(
            "/api/v1/pos/cash-sessions",
            json={"client_session_id": "t1", "opening_float_ars": "0.00"},
            headers=caja,
        )
        await client.post(
            f"/api/v1/pos/cash-sessions/{abrir.json()['id']}/close",
            json={"counted_cash_ars": "0.00"},
            headers=caja,
        )
        await _cobrar(client, caja, pid, "t1")
        detalle = await client.get(
            f"/api/v1/pos/cash-sessions/{abrir.json()['id']}", headers=auth_headers
        )
        assert detalle.json()["status"] == "CLOSED"

    async def test_reintentar_la_misma_venta_tardia_no_la_duplica(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """La cola offline reintenta: la conciliación no puede mostrarla dos veces."""
        caja = await _caja(client, auth_headers)
        pid = await _producto(client, auth_headers)
        abrir = await client.post(
            "/api/v1/pos/cash-sessions",
            json={"client_session_id": "t1", "opening_float_ars": "0.00"},
            headers=caja,
        )
        sid = abrir.json()["id"]
        await client.post(
            f"/api/v1/pos/cash-sessions/{sid}/close",
            json={"counted_cash_ars": "0.00"},
            headers=caja,
        )
        cuerpo = _venta(pid, "t1")
        primera = await client.post("/api/v1/pos/operations", json=cuerpo, headers=caja)
        assert primera.status_code == 201
        segunda = await client.post("/api/v1/pos/operations", json=cuerpo, headers=caja)
        assert segunda.status_code == 200  # el mismo ticket, recuperado

        detalle = await client.get(f"/api/v1/pos/cash-sessions/{sid}", headers=auth_headers)
        assert len(detalle.json()["late_sales"]) == 1


class TestDosCajeros:
    """Dos cajeros cierran el mismo día: cada arqueo es el suyo."""

    async def _cajero(
        self, client: AsyncClient, db_session: AsyncSession, tenant: Tenant, nombre: str
    ) -> dict[str, str]:
        user = User(
            tenant_id=tenant.tenant_id,
            email=f"{nombre}@caja.test",
            full_name=nombre,
            password_hash=hash_password("X1234567!"),
            role_code="CASHIER",
            is_active=True,
            pos_permissions={},
            pin_hash=hash_password(TEST_PIN),
        )
        db_session.add(user)
        await db_session.flush()
        token = create_access_token(
            {"sub": str(user.user_id), "tenant_id": str(tenant.tenant_id), "role_code": "CASHIER"}
        )
        return {"Authorization": f"Bearer {token}"}

    async def test_cada_cajero_cierra_su_turno_y_ve_solo_el_suyo(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        caja = await _caja(client, auth_headers)
        pid = await _producto(client, auth_headers)
        ana = {**await self._cajero(client, db_session, sample_tenant, "ana"),
               TERMINAL_HEADER: caja[TERMINAL_HEADER]}
        beto = {**await self._cajero(client, db_session, sample_tenant, "beto"),
                TERMINAL_HEADER: caja[TERMINAL_HEADER]}

        a = await client.post(
            "/api/v1/pos/cash-sessions",
            json={"client_session_id": "turno-ana", "opening_float_ars": "1000.00"},
            headers=ana,
        )
        b = await client.post(
            "/api/v1/pos/cash-sessions",
            json={"client_session_id": "turno-beto", "opening_float_ars": "2000.00"},
            headers=beto,
        )
        assert a.status_code == 201 and b.status_code == 201
        await _cobrar(client, ana, pid, "turno-ana", cantidad=1)
        await _cobrar(client, beto, pid, "turno-beto", cantidad=4)

        cierre_a = await client.post(
            f"/api/v1/pos/cash-sessions/{a.json()['id']}/close",
            json={"counted_cash_ars": "1100.00"},
            headers=ana,
        )
        cierre_b = await client.post(
            f"/api/v1/pos/cash-sessions/{b.json()['id']}/close",
            json={"counted_cash_ars": "2400.00"},
            headers=beto,
        )
        # Cada uno con SU fondo y SUS ventas. Si se mezclaran, los dos darían mal.
        assert cierre_a.json()["expected_cash_ars"] == "1100.00"
        assert cierre_b.json()["expected_cash_ars"] == "2400.00"

        # Y cada cajero ve sólo su turno.
        mios = await client.get("/api/v1/pos/cash-sessions", headers=ana)
        assert [s["client_session_id"] for s in mios.json()] == ["turno-ana"]
        # El dueño los ve todos.
        todos = await client.get("/api/v1/pos/cash-sessions", headers=auth_headers)
        assert {s["client_session_id"] for s in todos.json()} == {"turno-ana", "turno-beto"}

    async def test_un_cajero_no_cierra_el_turno_de_otro(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        """El arqueo queda firmado a nombre de quien lo cierra."""
        caja = await _caja(client, auth_headers)
        ana = {**await self._cajero(client, db_session, sample_tenant, "ana2"),
               TERMINAL_HEADER: caja[TERMINAL_HEADER]}
        beto = {**await self._cajero(client, db_session, sample_tenant, "beto2"),
                TERMINAL_HEADER: caja[TERMINAL_HEADER]}
        a = await client.post(
            "/api/v1/pos/cash-sessions",
            json={"client_session_id": "turno-ana2", "opening_float_ars": "0.00"},
            headers=ana,
        )
        resp = await client.post(
            f"/api/v1/pos/cash-sessions/{a.json()['id']}/close",
            json={"counted_cash_ars": "0.00"},
            headers=beto,
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "CASH_SESSION_NOT_OWN"

    async def test_el_encargado_si_puede_cerrar_el_de_un_cajero(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        """El cajero se fue sin cerrar: alguien tiene que poder hacerlo."""
        caja = await _caja(client, auth_headers)
        ana = {**await self._cajero(client, db_session, sample_tenant, "ana3"),
               TERMINAL_HEADER: caja[TERMINAL_HEADER]}
        a = await client.post(
            "/api/v1/pos/cash-sessions",
            json={"client_session_id": "turno-ana3", "opening_float_ars": "0.00"},
            headers=ana,
        )
        resp = await client.post(
            f"/api/v1/pos/cash-sessions/{a.json()['id']}/close",
            json={"counted_cash_ars": "0.00"},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text


class TestTurnoAbierto:
    async def test_el_parcial_de_un_turno_abierto_no_es_el_arqueo(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """Se muestra, pero no se guarda: cambia con cada venta."""
        caja = await _caja(client, auth_headers)
        pid = await _producto(client, auth_headers)
        abrir = await client.post(
            "/api/v1/pos/cash-sessions",
            json={"client_session_id": "t1", "opening_float_ars": "500.00"},
            headers=caja,
        )
        await _cobrar(client, caja, pid, "t1", cantidad=2)
        detalle = await client.get(
            f"/api/v1/pos/cash-sessions/{abrir.json()['id']}", headers=auth_headers
        )
        assert detalle.json()["expected_cash_now_ars"] == "700.00"
        # El arqueo sigue vacío: todavía no se firmó nada.
        assert detalle.json()["expected_cash_ars"] is None
        assert detalle.json()["counted_cash_ars"] is None


class TestAnulacion:
    async def test_un_ticket_anulado_no_suma_al_arqueo(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        sample_user: User,
        fake_redis,
    ) -> None:
        """Su plata volvió al cliente: contarla daría un sobrante falso."""
        caja = await _caja(client, auth_headers)
        pid = await _producto(client, auth_headers)
        abrir = await client.post(
            "/api/v1/pos/cash-sessions",
            json={"client_session_id": "t1", "opening_float_ars": "0.00"},
            headers=caja,
        )
        buena = await _cobrar(client, caja, pid, "t1", cantidad=2)
        anulada = await _cobrar(client, caja, pid, "t1", cantidad=9)
        # Anular pide PIN (`require_modify_access`): se abre la ventana igual
        # que en la suite de B11.
        await fake_redis.set(
            _pin_window_key(sample_tenant.tenant_id, sample_user.user_id), "1", ex=600
        )
        void = await client.post(
            f"/api/v1/pos/operations/{anulada['id']}/void",
            json={"reason": "Se arrepintió."},
            headers=caja,
        )
        assert void.status_code == 200, void.text

        cierre = await client.post(
            f"/api/v1/pos/cash-sessions/{abrir.json()['id']}/close",
            json={"counted_cash_ars": "200.00"},
            headers=caja,
        )
        assert cierre.json()["expected_cash_ars"] == "200.00"
        assert buena["id"] != anulada["id"]


def _venta_mixta(pid: str, turno: str) -> dict[str, Any]:
    """$1.000 en diez unidades: $600 en efectivo y $400 con débito."""
    return {
        **_venta(pid, turno, cantidad=10),
        "tenders": [
            {"payment_method": "cash", "amount_ars": "600.00"},
            {"payment_method": "debit_card", "amount_ars": "400.00"},
        ],
    }


class TestPagoMixtoEnElTurno:
    """B9: al cajón entra sólo la parte en efectivo de un pago mixto."""

    async def test_el_arqueo_espera_el_fondo_mas_la_parte_en_efectivo(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        caja = await _caja(client, auth_headers)
        pid = await _producto(client, auth_headers)
        abrir = await client.post(
            "/api/v1/pos/cash-sessions",
            json={"client_session_id": "t1", "opening_float_ars": "5000.00"},
            headers=caja,
        )
        sid = abrir.json()["id"]
        resp = await client.post(
            "/api/v1/pos/operations", json=_venta_mixta(pid, "t1"), headers=caja
        )
        assert resp.status_code == 201, resp.text

        abierto = await client.get(f"/api/v1/pos/cash-sessions/{sid}", headers=auth_headers)
        assert abierto.json()["expected_cash_now_ars"] == "5600.00"
        cierre = await client.post(
            f"/api/v1/pos/cash-sessions/{sid}/close",
            json={"counted_cash_ars": "5600.00"},
            headers=caja,
        )
        assert cierre.json()["expected_cash_ars"] == "5600.00"
        assert cierre.json()["difference_ars"] == "0.00"

    async def test_un_mixto_que_llega_tarde_concilia_solo_su_efectivo(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        caja = await _caja(client, auth_headers)
        pid = await _producto(client, auth_headers)
        abrir = await client.post(
            "/api/v1/pos/cash-sessions",
            json={"client_session_id": "t1", "opening_float_ars": "0.00"},
            headers=caja,
        )
        sid = abrir.json()["id"]
        await client.post(
            f"/api/v1/pos/cash-sessions/{sid}/close",
            json={"counted_cash_ars": "0.00"},
            headers=caja,
        )
        resp = await client.post(
            "/api/v1/pos/operations", json=_venta_mixta(pid, "t1"), headers=caja
        )
        assert resp.status_code == 201, resp.text

        detalle = await client.get(f"/api/v1/pos/cash-sessions/{sid}", headers=auth_headers)
        assert detalle.json()["expected_cash_ars"] == "0.00"
        assert [v["cash_ars"] for v in detalle.json()["late_sales"]] == ["600.00"]


async def _abrir(
    client: AsyncClient, caja: dict[str, str], turno: str, fondo: str
) -> dict[str, Any]:
    resp = await client.post(
        "/api/v1/pos/cash-sessions",
        json={"client_session_id": turno, "opening_float_ars": fondo},
        headers=caja,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _cajero_en_caja(
    db_session: AsyncSession, tenant: Tenant, caja: dict[str, str], nombre: str
) -> dict[str, str]:
    user = User(
        tenant_id=tenant.tenant_id,
        email=f"{nombre}-{uuid.uuid4().hex[:6]}@caja.test",
        full_name=nombre,
        password_hash=hash_password("X1234567!"),
        role_code="CASHIER",
        is_active=True,
        pos_permissions={},
    )
    db_session.add(user)
    await db_session.flush()
    token = create_access_token(
        {"sub": str(user.user_id), "tenant_id": str(tenant.tenant_id), "role_code": "CASHIER"}
    )
    return {"Authorization": f"Bearer {token}", TERMINAL_HEADER: caja[TERMINAL_HEADER]}


class TestFondoDeclarado:
    """La apertura puede ser sin red: una venta del turno puede llegar antes.

    El fondo tiene que quedar declarado en los dos órdenes, y una segunda
    apertura con otro fondo no puede contestar como si hubiera funcionado.
    """

    async def test_venta_antes_que_la_apertura_conserva_el_fondo(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        caja = await _caja(client, auth_headers)
        pid = await _producto(client, auth_headers)
        await _cobrar(client, caja, pid, "t-orden", cantidad=3)

        turno = await _abrir(client, caja, "t-orden", "5000.00")
        assert turno["opening_float_ars"] == "5000.00"
        assert turno["float_declared"] is True

        cierre = await client.post(
            f"/api/v1/pos/cash-sessions/{turno['id']}/close",
            json={"counted_cash_ars": "5300.00"},
            headers=caja,
        )
        assert cierre.json()["expected_cash_ars"] == "5300.00"
        assert cierre.json()["difference_ars"] == "0.00"

    async def test_apertura_antes_que_la_venta_conserva_el_fondo(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        caja = await _caja(client, auth_headers)
        pid = await _producto(client, auth_headers)
        turno = await _abrir(client, caja, "t-orden2", "5000.00")
        await _cobrar(client, caja, pid, "t-orden2", cantidad=3)

        cierre = await client.post(
            f"/api/v1/pos/cash-sessions/{turno['id']}/close",
            json={"counted_cash_ars": "5300.00"},
            headers=caja,
        )
        assert cierre.json()["expected_cash_ars"] == "5300.00"

    async def test_turno_adoptado_no_tiene_fondo_declarado(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        caja = await _caja(client, auth_headers)
        pid = await _producto(client, auth_headers)
        await _cobrar(client, caja, pid, "t-adoptado")
        listado = await client.get("/api/v1/pos/cash-sessions", headers=auth_headers)
        fila = next(s for s in listado.json() if s["client_session_id"] == "t-adoptado")
        assert fila["float_declared"] is False
        assert fila["opening_float_ars"] == "0.00"

    async def test_segunda_apertura_con_otro_fondo_devuelve_el_real(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """Gana la primera declaración y la caja se entera del fondo real."""
        caja = await _caja(client, auth_headers)
        await _abrir(client, caja, "t-dos", "5000.00")
        otra = await _abrir(client, caja, "t-dos", "8000.00")
        assert otra["opening_float_ars"] == "5000.00"
        assert otra["float_declared"] is True

    async def test_declarar_cero_es_declarar(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """$0 es un fondo válido: después no lo pisa otra apertura."""
        caja = await _caja(client, auth_headers)
        await _abrir(client, caja, "t-cero", "0.00")
        otra = await _abrir(client, caja, "t-cero", "3000.00")
        assert otra["opening_float_ars"] == "0.00"
        assert otra["float_declared"] is True

    async def test_la_apertura_no_cambia_el_fondo_de_un_turno_cerrado(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        caja = await _caja(client, auth_headers)
        pid = await _producto(client, auth_headers)
        await _cobrar(client, caja, pid, "t-cerrado")
        listado = await client.get("/api/v1/pos/cash-sessions", headers=auth_headers)
        sid = next(s["id"] for s in listado.json() if s["client_session_id"] == "t-cerrado")
        await client.post(
            f"/api/v1/pos/cash-sessions/{sid}/close",
            json={"counted_cash_ars": "100.00"},
            headers=caja,
        )
        tarde = await _abrir(client, caja, "t-cerrado", "5000.00")
        assert tarde["status"] == "CLOSED"
        assert tarde["opening_float_ars"] == "0.00"
        assert tarde["expected_cash_ars"] == "100.00"


class TestCerrarSinArqueo:
    """Los turnos que nadie va a contar se cierran sin inventar un conteo."""

    async def _pin(self, fake_redis, tenant: Tenant, user: User) -> None:
        await fake_redis.set(_pin_window_key(tenant.tenant_id, user.user_id), "1", ex=600)

    async def test_abandonar_deja_los_importes_en_null_y_audita(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        sample_user: User,
        fake_redis,
    ) -> None:
        caja = await _caja(client, auth_headers)
        pid = await _producto(client, auth_headers)
        await _cobrar(client, caja, pid, "t-basura")
        listado = await client.get("/api/v1/pos/cash-sessions", headers=auth_headers)
        sid = next(s["id"] for s in listado.json() if s["client_session_id"] == "t-basura")
        await self._pin(fake_redis, sample_tenant, sample_user)

        resp = await client.post(
            f"/api/v1/pos/cash-sessions/{sid}/abandon",
            json={"reason": "Turno de una recarga de la pantalla."},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        cuerpo = resp.json()
        assert cuerpo["status"] == "ABANDONED"
        assert cuerpo["expected_cash_ars"] is None
        assert cuerpo["counted_cash_ars"] is None
        assert cuerpo["difference_ars"] is None
        assert cuerpo["closed_at"] is not None

        auditoria = (
            await db_session.execute(
                select(DecisionAuditLog).where(
                    DecisionAuditLog.decision_type == "POS_CASH_SESSION_ABANDONED"
                )
            )
        ).scalar_one()
        assert auditoria.decision_data["reason"] == "Turno de una recarga de la pantalla."
        assert auditoria.decision_data["float_declared"] is False

    async def test_abandonar_exige_motivo(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        sample_tenant: Tenant,
        sample_user: User,
        fake_redis,
    ) -> None:
        caja = await _caja(client, auth_headers)
        turno = await _abrir(client, caja, "t-motivo", "0.00")
        await self._pin(fake_redis, sample_tenant, sample_user)
        resp = await client.post(
            f"/api/v1/pos/cash-sessions/{turno['id']}/abandon",
            json={"reason": "   "},
            headers=auth_headers,
        )
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "ABANDON_REASON_REQUIRED"

    async def test_abandonar_sin_pin_pide_pin(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        sample_tenant: Tenant,
        sample_user: User,
        fake_redis,
    ) -> None:
        caja = await _caja(client, auth_headers)
        turno = await _abrir(client, caja, "t-sin-pin", "0.00")
        # Habilitar la caja abre la ventana de PIN del dueño: se cierra para
        # probar que cerrar sin arqueo la exige.
        await fake_redis.delete(_pin_window_key(sample_tenant.tenant_id, sample_user.user_id))
        resp = await client.post(
            f"/api/v1/pos/cash-sessions/{turno['id']}/abandon",
            json={"reason": "Basura."},
            headers=auth_headers,
        )
        assert resp.status_code == 428

    async def test_abandonar_dos_veces_o_un_cerrado_da_409(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        sample_tenant: Tenant,
        sample_user: User,
        fake_redis,
    ) -> None:
        caja = await _caja(client, auth_headers)
        uno = await _abrir(client, caja, "t-409a", "0.00")
        dos = await _abrir(client, caja, "t-409b", "0.00")
        await self._pin(fake_redis, sample_tenant, sample_user)
        primero = await client.post(
            f"/api/v1/pos/cash-sessions/{uno['id']}/abandon",
            json={"reason": "Basura."},
            headers=auth_headers,
        )
        assert primero.status_code == 200
        segundo = await client.post(
            f"/api/v1/pos/cash-sessions/{uno['id']}/abandon",
            json={"reason": "Basura."},
            headers=auth_headers,
        )
        assert segundo.status_code == 409
        assert segundo.json()["detail"]["code"] == "CASH_SESSION_ALREADY_CLOSED"

        await client.post(
            f"/api/v1/pos/cash-sessions/{dos['id']}/close",
            json={"counted_cash_ars": "0.00"},
            headers=caja,
        )
        cerrado = await client.post(
            f"/api/v1/pos/cash-sessions/{dos['id']}/abandon",
            json={"reason": "Basura."},
            headers=auth_headers,
        )
        assert cerrado.status_code == 409
        # Y cerrar un abandonado tampoco: su "no hubo arqueo" ya está firmado.
        tarde = await client.post(
            f"/api/v1/pos/cash-sessions/{uno['id']}/close",
            json={"counted_cash_ars": "0.00"},
            headers=caja,
        )
        assert tarde.status_code == 409

    async def test_una_venta_a_un_turno_abandonado_queda_como_tardia(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        sample_tenant: Tenant,
        sample_user: User,
        fake_redis,
    ) -> None:
        caja = await _caja(client, auth_headers)
        pid = await _producto(client, auth_headers)
        turno = await _abrir(client, caja, "t-aband", "0.00")
        await self._pin(fake_redis, sample_tenant, sample_user)
        await client.post(
            f"/api/v1/pos/cash-sessions/{turno['id']}/abandon",
            json={"reason": "Basura."},
            headers=auth_headers,
        )
        venta = await _cobrar(client, caja, pid, "t-aband", cantidad=2)
        detalle = await client.get(
            f"/api/v1/pos/cash-sessions/{turno['id']}", headers=auth_headers
        )
        cuerpo = detalle.json()
        assert cuerpo["status"] == "ABANDONED"
        assert cuerpo["expected_cash_ars"] is None
        assert [t["operation_id"] for t in cuerpo["late_sales"]] == [venta["id"]]
        assert cuerpo["late_sales"][0]["cash_ars"] == "200.00"


class TestArqueoCiego:
    """El cajero no ve cuánto 'debería' haber mientras el turno está abierto."""

    async def test_el_cajero_no_recibe_el_esperado_y_el_dueno_si(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        caja = await _caja(client, auth_headers)
        pid = await _producto(client, auth_headers)
        ana = await _cajero_en_caja(db_session, sample_tenant, caja, "ana")
        turno = await _abrir(client, ana, "t-ciego", "1000.00")
        await _cobrar(client, ana, pid, "t-ciego", cantidad=2)

        del_cajero = await client.get(f"/api/v1/pos/cash-sessions/{turno['id']}", headers=ana)
        assert del_cajero.status_code == 200
        assert del_cajero.json()["expected_cash_now_ars"] is None
        listado_cajero = await client.get("/api/v1/pos/cash-sessions?open=true", headers=ana)
        assert all(s["expected_cash_now_ars"] is None for s in listado_cajero.json())
        assert all(s["sales_count"] is None for s in listado_cajero.json())

        del_dueno = await client.get(
            f"/api/v1/pos/cash-sessions/{turno['id']}", headers=auth_headers
        )
        assert del_dueno.json()["expected_cash_now_ars"] == "1200.00"

        # Firmado el conteo, el cajero sí ve el resultado.
        cierre = await client.post(
            f"/api/v1/pos/cash-sessions/{turno['id']}/close",
            json={"counted_cash_ars": "1150.00"},
            headers=ana,
        )
        assert cierre.json()["expected_cash_ars"] == "1200.00"
        assert cierre.json()["difference_ars"] == "-50.00"


class TestListadoDelEncargado:
    async def test_trae_cajero_efectivo_y_tickets_y_filtra_por_caja(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        caja = await _caja(client, auth_headers)
        otra = await _caja(client, auth_headers)
        pid = await _producto(client, auth_headers)
        ana = await _cajero_en_caja(db_session, sample_tenant, caja, "Ana")
        await _abrir(client, ana, "t-lista", "500.00")
        await _cobrar(client, ana, pid, "t-lista", cantidad=2)
        await _cobrar(client, ana, pid, "t-lista", cantidad=1, metodo="debit_card")
        await _abrir(client, otra, "t-otra-caja", "0.00")

        listado = await client.get("/api/v1/pos/cash-sessions?open=true", headers=auth_headers)
        fila = next(s for s in listado.json() if s["client_session_id"] == "t-lista")
        assert fila["cashier_name"] == "Ana"
        assert fila["expected_cash_now_ars"] == "700.00"
        assert fila["sales_count"] == 2
        vacia = next(s for s in listado.json() if s["client_session_id"] == "t-otra-caja")
        assert vacia["sales_count"] == 0

        por_caja = await client.get(
            f"/api/v1/pos/cash-sessions?open=true&terminal_id={fila['terminal_id']}",
            headers=auth_headers,
        )
        assert [s["client_session_id"] for s in por_caja.json()] == ["t-lista"]
