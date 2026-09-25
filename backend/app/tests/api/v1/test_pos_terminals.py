"""Compuertas de las terminales de caja (B6).

La credencial identifica la PC, no a la persona. Lo que se protege: que el
secreto no se pueda recuperar, que sólo el dueño habilite cajas, que una caja
dada de baja no pueda cobrar ventas NUEVAS, y que un cajero no cobre fuera de una
caja habilitada.
"""

from __future__ import annotations

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
from app.persistence.models.pos_operation import PosOperation
from app.persistence.models.pos_terminal import PosTerminal
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.tests.conftest import TEST_PIN, _pin_window_key
from app.utils.security import create_access_token, hash_password

_FECHA = f"{today_ar().isoformat()}T10:00:00"


@pytest.fixture(autouse=True)
def _sin_celery(mock_score_trigger):
    with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
        yield


async def _habilitar(client: AsyncClient, headers: dict[str, str], name: str = "Caja 1") -> dict:
    resp = await client.post("/api/v1/pos/terminals", json={"name": name}, headers=headers)
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def _producto(client: AsyncClient, headers: dict[str, str]) -> str:
    resp = await client.post(
        "/api/v1/products",
        json={"name": f"Yerba {uuid.uuid4().hex[:4]}", "sale_price_ars": "100.00",
              "stock_units": 10},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def _venta(pid: str) -> dict[str, Any]:
    return {
        "client_operation_id": f"op-{uuid.uuid4().hex[:16]}",
        "operation_date": _FECHA,
        "items": [{"product_id": pid, "quantity": 1}],
        "tenders": [{"payment_method": "cash", "amount_ars": "100.00"}],
    }


async def _usuario(
    db_session: AsyncSession, tenant: Tenant, fake_redis: Any, rol: str
) -> dict[str, str]:
    user = User(
        user_id=uuid.uuid4(), tenant_id=tenant.tenant_id,
        email=f"{rol.lower()}-{uuid.uuid4().hex[:6]}@kiosco.com", full_name=rol,
        password_hash=hash_password("Secure123"), role_code=rol, is_active=True,
        pin_hash=hash_password(TEST_PIN),
    )
    db_session.add(user)
    await db_session.commit()
    await fake_redis.set(_pin_window_key(tenant.tenant_id, user.user_id), "1", ex=600)
    token = create_access_token(
        {"sub": str(user.user_id), "tenant_id": str(tenant.tenant_id), "role_code": rol}
    )
    return {"Authorization": f"Bearer {token}"}


class TestHabilitar:
    async def test_el_secreto_se_ve_una_sola_vez(
        self, client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
    ) -> None:
        alta = await _habilitar(client, auth_headers)
        assert len(alta["secret"]) >= 40

        lista = await client.get("/api/v1/pos/terminals", headers=auth_headers)
        assert lista.status_code == 200
        assert "secret" not in lista.json()[0]

        # Tampoco en la base: sólo el hash.
        fila = await db_session.get(PosTerminal, uuid.UUID(alta["id"]))
        assert fila is not None
        assert alta["secret"] not in fila.credential_hash

    async def test_queda_auditado(
        self, client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
    ) -> None:
        await _habilitar(client, auth_headers)
        filas = (
            await db_session.execute(
                select(DecisionAuditLog).where(
                    DecisionAuditLog.decision_type == "POS_TERMINAL_ENROLLED"
                )
            )
        ).scalars().all()
        assert len(filas) == 1

    async def test_nombre_repetido_entre_activas(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        primera = await _habilitar(client, auth_headers, "Mostrador")
        repetida = await client.post(
            "/api/v1/pos/terminals", json={"name": "Mostrador"}, headers=auth_headers
        )
        assert repetida.status_code == 409
        # Dada de baja, el nombre se libera.
        await client.post(f"/api/v1/pos/terminals/{primera['id']}/disable", headers=auth_headers)
        await _habilitar(client, auth_headers, "Mostrador")

    @pytest.mark.parametrize("rol", ["ADMIN", "CASHIER"])
    async def test_solo_el_duenio_habilita(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        fake_redis: Any,
        rol: str,
    ) -> None:
        headers = await _usuario(db_session, sample_tenant, fake_redis, rol)
        resp = await client.post("/api/v1/pos/terminals", json={"name": "X"}, headers=headers)
        assert resp.status_code == 403

    async def test_dar_de_baja_dos_veces(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        alta = await _habilitar(client, auth_headers)
        url = f"/api/v1/pos/terminals/{alta['id']}/disable"
        primera = await client.post(url, headers=auth_headers)
        segunda = await client.post(url, headers=auth_headers)
        assert primera.status_code == segunda.status_code == 200
        # El mismo instante: la segunda baja no lo movió. Se compara sin la zona
        # porque SQLite devuelve la fila releída sin ella.
        assert primera.json()["disabled_at"][:26] == segunda.json()["disabled_at"][:26]


class TestCobrarDesdeUnaTerminal:
    async def test_la_venta_guarda_la_terminal_y_su_ultimo_uso(
        self, client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
    ) -> None:
        alta = await _habilitar(client, auth_headers)
        pid = await _producto(client, auth_headers)
        resp = await client.post(
            "/api/v1/pos/operations",
            json=_venta(pid),
            headers={**auth_headers, TERMINAL_HEADER: alta["secret"]},
        )
        assert resp.status_code == 201, resp.text
        op = await db_session.get(PosOperation, uuid.UUID(resp.json()["id"]))
        assert op is not None
        assert str(op.terminal_id) == alta["id"]
        terminal = await db_session.get(PosTerminal, uuid.UUID(alta["id"]))
        assert terminal is not None
        await db_session.refresh(terminal)
        assert terminal.last_seen_at is not None

    async def test_una_caja_dada_de_baja_no_cobra(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        alta = await _habilitar(client, auth_headers)
        await client.post(f"/api/v1/pos/terminals/{alta['id']}/disable", headers=auth_headers)
        pid = await _producto(client, auth_headers)
        resp = await client.post(
            "/api/v1/pos/operations",
            json=_venta(pid),
            headers={**auth_headers, TERMINAL_HEADER: alta["secret"]},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "TERMINAL_DISABLED"
        prod = await client.get(f"/api/v1/products/{pid}", headers=auth_headers)
        assert prod.json()["stock_units"] == 10, "no puede haber descontado stock"

    async def test_secreto_desconocido(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        pid = await _producto(client, auth_headers)
        resp = await client.post(
            "/api/v1/pos/operations",
            json=_venta(pid),
            headers={**auth_headers, TERMINAL_HEADER: "no-es-un-secreto"},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "TERMINAL_UNKNOWN"

    async def test_la_caja_de_otro_negocio_es_desconocida(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        second_auth_headers: dict[str, str],
    ) -> None:
        ajena = await _habilitar(client, second_auth_headers)
        pid = await _producto(client, auth_headers)
        resp = await client.post(
            "/api/v1/pos/operations",
            json=_venta(pid),
            headers={**auth_headers, TERMINAL_HEADER: ajena["secret"]},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "TERMINAL_UNKNOWN"

    async def test_el_duenio_cobra_sin_terminal(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        pid = await _producto(client, auth_headers)
        resp = await client.post("/api/v1/pos/operations", json=_venta(pid), headers=auth_headers)
        assert resp.status_code == 201, resp.text

    async def test_reintento_tras_la_baja_devuelve_el_ticket(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """La venta se hizo: dar de baja la PC después no puede negar el ticket."""
        alta = await _habilitar(client, auth_headers)
        pid = await _producto(client, auth_headers)
        cuerpo = _venta(pid)
        con_caja = {**auth_headers, TERMINAL_HEADER: alta["secret"]}
        primera = await client.post("/api/v1/pos/operations", json=cuerpo, headers=con_caja)
        assert primera.status_code == 201

        await client.post(f"/api/v1/pos/terminals/{alta['id']}/disable", headers=auth_headers)
        reintento = await client.post("/api/v1/pos/operations", json=cuerpo, headers=con_caja)
        assert reintento.status_code == 200, reintento.text
        assert reintento.json()["id"] == primera.json()["id"]


class TestCajeroSinTerminal:
    async def _cajero_sin_caja(
        self, db_session: AsyncSession, tenant: Tenant, fake_redis: Any
    ) -> dict[str, str]:
        headers = await _usuario(db_session, tenant, fake_redis, "CASHIER")
        user = (
            await db_session.execute(select(User).where(User.role_code == "CASHIER"))
        ).scalars().first()
        assert user is not None
        user.pos_permissions = {"void_ticket": True, "learn_barcode": True}
        await db_session.commit()
        return headers

    async def test_no_cobra(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        fake_redis: Any,
    ) -> None:
        pid = await _producto(client, auth_headers)
        headers = await self._cajero_sin_caja(db_session, sample_tenant, fake_redis)
        resp = await client.post("/api/v1/pos/operations", json=_venta(pid), headers=headers)
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "TERMINAL_REQUIRED"

    async def test_no_anula(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        fake_redis: Any,
    ) -> None:
        pid = await _producto(client, auth_headers)
        op = await client.post("/api/v1/pos/operations", json=_venta(pid), headers=auth_headers)
        headers = await self._cajero_sin_caja(db_session, sample_tenant, fake_redis)
        resp = await client.post(
            f"/api/v1/pos/operations/{op.json()['id']}/void",
            json={"reason": "x"},
            headers=headers,
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "TERMINAL_REQUIRED"

    async def test_no_vincula_codigos(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        fake_redis: Any,
    ) -> None:
        pid = await _producto(client, auth_headers)
        headers = await self._cajero_sin_caja(db_session, sample_tenant, fake_redis)
        resp = await client.post(
            f"/api/v1/products/{pid}/barcode", json={"barcode": "4006381333931"}, headers=headers
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "TERMINAL_REQUIRED"
