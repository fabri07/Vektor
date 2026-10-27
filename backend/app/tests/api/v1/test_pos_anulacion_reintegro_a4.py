"""A-4: anular un ticket de otro turno u otro día deja el reintegro en el cajón de HOY.

Antes, anular un ticket de un turno ya cerrado (o de ayer) sacaba la venta de
su día —cuyo arqueo ya estaba firmado— y no registraba la plata que hoy salía
del cajón: el arqueo del turno abierto y el cierre diario de hoy daban un
faltante sin explicación. Lo que se protege acá:

1. El arqueo firmado del turno de la venta no cambia.
2. El reintegro resta en el turno abierto de cuyo cajón salió, y el arqueo cuadra.
3. El cierre diario lo resta HOY y se lo devuelve al día de la venta.
4. Sin turno abierto, un reintegro en efectivo da 409 y no se anula nada.
"""

import unittest.mock
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.business_time import today_ar
from app.domain.pos_terminal import TERMINAL_HEADER
from app.persistence.models.pos_cash_session import PosCashRefund, PosCashSession
from app.persistence.models.pos_operation import PosOperation
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.models.user import User
from app.tests.conftest import _pin_window_key

_HOY = today_ar()
_AYER = _HOY - timedelta(days=1)


@pytest.fixture(autouse=True)
def _sin_celery(mock_score_trigger):
    with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
        yield


@pytest.fixture(autouse=True)
async def _pin(fake_redis, sample_tenant: Tenant, sample_user: User) -> None:
    # Anular pide PIN (`require_modify_access`): ventana abierta, como en B10.
    await fake_redis.set(
        _pin_window_key(sample_tenant.tenant_id, sample_user.user_id), "1", ex=600
    )


async def _caja(client: AsyncClient, headers: dict[str, str]) -> dict[str, str]:
    alta = await client.post(
        "/api/v1/pos/terminals",
        json={"name": f"Caja {uuid.uuid4().hex[:4]}"},
        headers=headers,
    )
    assert alta.status_code == 201, alta.text
    return {**headers, TERMINAL_HEADER: alta.json()["secret"]}


async def _producto(client: AsyncClient, headers: dict[str, str]) -> str:
    resp = await client.post(
        "/api/v1/products",
        json={
            "name": f"Yerba {uuid.uuid4().hex[:5]}",
            "sale_price_ars": "100.00",
            "stock_units": 100,
            "unit_cost_ars": "50.00",
            "acquired_at": "2026-01-01T09:00:00",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def _abrir(client: AsyncClient, caja: dict[str, str], turno: str, fondo: str) -> str:
    resp = await client.post(
        "/api/v1/pos/cash-sessions",
        json={"client_session_id": turno, "opening_float_ars": fondo},
        headers=caja,
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def _cobrar(
    client: AsyncClient,
    caja: dict[str, str],
    pid: str,
    turno: str,
    *,
    cantidad: int,
    dia: Any = _HOY,
    metodo: str = "cash",
) -> str:
    resp = await client.post(
        "/api/v1/pos/operations",
        json={
            "client_operation_id": f"op-{uuid.uuid4().hex[:16]}",
            "operation_date": f"{dia.isoformat()}T10:00:00",
            "client_session_id": turno,
            "items": [{"product_id": pid, "quantity": cantidad, "discount_ars": "0.00"}],
            "tenders": [{"payment_method": metodo, "amount_ars": f"{cantidad * 100}.00"}],
        },
        headers=caja,
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def _cerrar(client: AsyncClient, caja: dict[str, str], sid: str, contado: str) -> dict:
    resp = await client.post(
        f"/api/v1/pos/cash-sessions/{sid}/close",
        json={"counted_cash_ars": contado},
        headers=caja,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _anular(
    client: AsyncClient, caja: dict[str, str], op_id: str, **extra: Any
) -> Any:
    return await client.post(
        f"/api/v1/pos/operations/{op_id}/void",
        json={"reason": "El cliente lo devolvió.", **extra},
        headers=caja,
    )


async def _esperado_ahora(client: AsyncClient, headers: dict[str, str], sid: str) -> str:
    resp = await client.get(f"/api/v1/pos/cash-sessions/{sid}", headers=headers)
    assert resp.status_code == 200, resp.text
    return str(resp.json()["expected_cash_now_ars"])


async def _efectivo_del_dia(client: AsyncClient, headers: dict[str, str], dia: Any) -> float:
    resp = await client.get(
        f"/api/v1/cash-closes/preview?close_date={dia.isoformat()}", headers=headers
    )
    assert resp.status_code == 200, resp.text
    por_metodo = {b["payment_method"]: b["expected_ars"] for b in resp.json()["breakdown"]}
    return float(por_metodo.get("cash", 0.0))


async def _reintegros(db_session: AsyncSession) -> list[PosCashRefund]:
    return list((await db_session.execute(select(PosCashRefund))).scalars().all())


class TestTurnoCerrado:
    async def test_el_reintegro_resta_en_el_turno_abierto_y_el_cerrado_no_cambia(
        self, client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
    ) -> None:
        caja = await _caja(client, auth_headers)
        pid = await _producto(client, auth_headers)
        t1 = await _abrir(client, caja, "t1", "0.00")
        op = await _cobrar(client, caja, pid, "t1", cantidad=5)
        firmado = await _cerrar(client, caja, t1, "500.00")
        assert firmado["expected_cash_ars"] == "500.00"

        t2 = await _abrir(client, caja, "t2", "1000.00")
        resp = await _anular(client, caja, op)
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "VOIDED"

        # El arqueo firmado de t1 no se reescribe.
        cerrado = (
            await db_session.execute(
                select(PosCashSession)
                .where(PosCashSession.id == uuid.UUID(t1))
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        assert (cerrado.expected_cash_ars, cerrado.counted_cash_ars, cerrado.difference_ars) == (
            Decimal("500.00"),
            Decimal("500.00"),
            Decimal("0.00"),
        )
        # La plata salió del cajón de t2: 1000 − 500.
        assert await _esperado_ahora(client, auth_headers, t2) == "500.00"
        [fila] = await _reintegros(db_session)
        assert fila.session_id == uuid.UUID(t2)
        assert fila.origin_client_session_id == "t1"
        assert fila.cash_ars == Decimal("500.00")
        assert (fila.sale_date, fila.refund_date) == (_HOY, _HOY)

        # Y el arqueo de t2 cuadra contando lo que de verdad hay en el cajón.
        cierre = await _cerrar(client, caja, t2, "500.00")
        assert cierre["expected_cash_ars"] == "500.00"
        assert cierre["difference_ars"] == "0.00"

    async def test_sin_turno_abierto_da_409_y_no_anula_nada(
        self, client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
    ) -> None:
        caja = await _caja(client, auth_headers)
        pid = await _producto(client, auth_headers)
        t1 = await _abrir(client, caja, "t1", "0.00")
        op = await _cobrar(client, caja, pid, "t1", cantidad=5)
        await _cerrar(client, caja, t1, "500.00")

        resp = await _anular(client, caja, op)
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["code"] == "VOID_NEEDS_OPEN_CASH_SESSION"

        operacion = await db_session.get(
            PosOperation, uuid.UUID(op), populate_existing=True
        )
        assert operacion is not None and operacion.status == "COMPLETED"
        vivas = await db_session.scalar(
            select(func.count(SaleEntry.id)).where(SaleEntry.voided_at.is_(None))
        )
        assert vivas == 1
        assert await _reintegros(db_session) == []

    async def test_un_ticket_con_tarjeta_no_pide_turno_ni_deja_reintegro(
        self, client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
    ) -> None:
        """La plata de la tarjeta no está en el cajón: no hay de dónde sacarla."""
        caja = await _caja(client, auth_headers)
        pid = await _producto(client, auth_headers)
        t1 = await _abrir(client, caja, "t1", "0.00")
        op = await _cobrar(client, caja, pid, "t1", cantidad=5, metodo="debit_card")
        await _cerrar(client, caja, t1, "0.00")

        resp = await _anular(client, caja, op)
        assert resp.status_code == 200, resp.text
        assert await _reintegros(db_session) == []

    async def test_anular_dos_veces_no_duplica_el_reintegro(
        self, client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
    ) -> None:
        caja = await _caja(client, auth_headers)
        pid = await _producto(client, auth_headers)
        t1 = await _abrir(client, caja, "t1", "0.00")
        op = await _cobrar(client, caja, pid, "t1", cantidad=5)
        await _cerrar(client, caja, t1, "500.00")
        t2 = await _abrir(client, caja, "t2", "1000.00")

        assert (await _anular(client, caja, op)).status_code == 200
        assert (await _anular(client, caja, op)).status_code == 200
        assert len(await _reintegros(db_session)) == 1
        assert await _esperado_ahora(client, auth_headers, t2) == "500.00"

    async def test_un_turno_indicado_que_no_esta_abierto_da_409(
        self, client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
    ) -> None:
        caja = await _caja(client, auth_headers)
        pid = await _producto(client, auth_headers)
        t1 = await _abrir(client, caja, "t1", "0.00")
        op = await _cobrar(client, caja, pid, "t1", cantidad=5)
        await _cerrar(client, caja, t1, "500.00")
        await _abrir(client, caja, "t2", "1000.00")

        resp = await _anular(client, caja, op, client_session_id="t1")
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["code"] == "VOID_CASH_SESSION_NOT_OPEN"
        assert await _reintegros(db_session) == []


class TestOtroDia:
    async def test_ticket_de_ayer_cuadra_el_turno_y_los_dos_cierres_diarios(
        self, client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
    ) -> None:
        """El turno cruzó la medianoche: la venta es de ayer y se devuelve hoy."""
        caja = await _caja(client, auth_headers)
        pid = await _producto(client, auth_headers)
        t1 = await _abrir(client, caja, "t1", "1000.00")
        op = await _cobrar(client, caja, pid, "t1", cantidad=3, dia=_AYER)
        ayer_antes = await _efectivo_del_dia(client, auth_headers, _AYER)
        hoy_antes = await _efectivo_del_dia(client, auth_headers, _HOY)
        assert ayer_antes == 300.0

        resp = await _anular(client, caja, op)
        assert resp.status_code == 200, resp.text

        [fila] = await _reintegros(db_session)
        assert (fila.sale_date, fila.refund_date) == (_AYER, _HOY)
        assert fila.session_id == uuid.UUID(t1)
        # El efectivo entró ayer y salió hoy: el día de ayer no cambia y hoy resta.
        assert await _efectivo_del_dia(client, auth_headers, _AYER) == ayer_antes
        assert await _efectivo_del_dia(client, auth_headers, _HOY) == hoy_antes - 300.0
        # En el turno: entró y salió del mismo cajón, queda el fondo.
        assert await _esperado_ahora(client, auth_headers, t1) == "1000.00"


class TestOtroTurnoAbierto:
    async def test_el_turno_de_la_venta_conserva_su_plata_y_resta_el_que_devuelve(
        self, client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
    ) -> None:
        """Se cobró en la caja A y se devuelve desde la caja B, las dos abiertas."""
        caja_a = await _caja(client, auth_headers)
        caja_b = await _caja(client, auth_headers)
        pid = await _producto(client, auth_headers)
        ta = await _abrir(client, caja_a, "ta", "0.00")
        tb = await _abrir(client, caja_b, "tb", "1000.00")
        op = await _cobrar(client, caja_a, pid, "ta", cantidad=4)

        resp = await _anular(client, caja_b, op)
        assert resp.status_code == 200, resp.text

        # El cajón de A todavía tiene los 400; el de B los entregó.
        assert await _esperado_ahora(client, auth_headers, ta) == "400.00"
        assert await _esperado_ahora(client, auth_headers, tb) == "600.00"
        listado = await client.get("/api/v1/pos/cash-sessions?open=true", headers=auth_headers)
        por_id = {s["id"]: s["expected_cash_now_ars"] for s in listado.json()}
        assert (por_id[ta], por_id[tb]) == ("400.00", "600.00")


class TestMismoTurnoMismoDia:
    async def test_no_deja_reintegro(
        self, client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
    ) -> None:
        """El caso de siempre: el ticket anulado deja de sumar y alcanza."""
        caja = await _caja(client, auth_headers)
        pid = await _producto(client, auth_headers)
        t1 = await _abrir(client, caja, "t1", "1000.00")
        op = await _cobrar(client, caja, pid, "t1", cantidad=2)

        assert (await _anular(client, caja, op)).status_code == 200
        assert await _reintegros(db_session) == []
        assert await _esperado_ahora(client, auth_headers, t1) == "1000.00"
