"""Un reintento tiene que poder recuperar el ticket, no solo evitar el duplicado.

`claim_idempotency_key` contesta "esa clave ya se usó" y nada más. Con una caja
de por medio eso deja sin resolver el caso que produce el doble cobro: el
servidor guardó la venta y el cliente nunca recibió la respuesta (corte justo
después del commit, timeout del proxy, el navegador que se cerró). Un 409 vacío
deja al cajero sin saber si cobró.

Y deja pasar el opuesto: la misma clave con OTRO contenido no es un reintento,
es una clave reusada por error, y tragársela hace desaparecer la segunda venta.
"""

from __future__ import annotations

import unittest.mock
from typing import Any

import pytest
from httpx import AsyncClient

from app.domain.business_time import today_ar

pytestmark = pytest.mark.asyncio

# Dentro de la ventana por defecto de `GET /sales` (últimos 30 días): una fecha
# fija y vieja haría que el listado devuelva vacío y los asserts midan otra cosa.
_FECHA = f"{today_ar().isoformat()}T10:00:00"


async def _producto(client: AsyncClient, headers: dict[str, Any], nombre: str, stock: int) -> str:
    resp = await client.post(
        "/api/v1/products",
        json={"name": nombre, "sale_price_ars": "1000.00", "stock_units": stock},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def _carrito(pid: str, *, quantity: int = 2, unit_price: str = "1000.00") -> dict[str, Any]:
    return {
        "payment_method": "cash",
        "transaction_date": _FECHA,
        "items": [{"product_id": pid, "quantity": quantity, "unit_price": unit_price}],
    }


class TestIdempotenciaRecuperable:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
            yield

    async def test_el_reintento_devuelve_el_ticket_original(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """El caso que produce el doble cobro: respuesta perdida después del commit."""
        pid = await _producto(client, auth_headers, "Coca 500", 10)
        headers = {**auth_headers, "Idempotency-Key": "ticket-001"}

        primera = await client.post(
            "/api/v1/sales/manual-batch", json=_carrito(pid), headers=headers
        )
        assert primera.status_code == 201, primera.text

        segunda = await client.post(
            "/api/v1/sales/manual-batch", json=_carrito(pid), headers=headers
        )
        # 200 y no 201: el recurso ya existía, esta petición no creó nada.
        assert segunda.status_code == 200, segunda.text
        assert segunda.json() == primera.json()

    async def test_el_reintento_no_crea_una_segunda_venta_ni_descuenta_de_nuevo(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        pid = await _producto(client, auth_headers, "Fernet", 10)
        headers = {**auth_headers, "Idempotency-Key": "ticket-002"}

        await client.post("/api/v1/sales/manual-batch", json=_carrito(pid), headers=headers)
        await client.post("/api/v1/sales/manual-batch", json=_carrito(pid), headers=headers)

        ventas = (await client.get("/api/v1/sales", headers=auth_headers)).json()
        assert len([v for v in ventas if v["product_id"] == pid]) == 1
        producto = (await client.get(f"/api/v1/products/{pid}", headers=auth_headers)).json()
        assert producto["stock_units"] == 8  # 10 - 2, una sola vez

    async def test_la_misma_clave_con_otro_contenido_se_rechaza(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """No es un reintento: es una clave reusada, y tragarla pierde la 2ª venta."""
        pid = await _producto(client, auth_headers, "Quilmes", 20)
        headers = {**auth_headers, "Idempotency-Key": "ticket-003"}

        primera = await client.post(
            "/api/v1/sales/manual-batch", json=_carrito(pid, quantity=2), headers=headers
        )
        assert primera.status_code == 201, primera.text

        otra = await client.post(
            "/api/v1/sales/manual-batch", json=_carrito(pid, quantity=5), headers=headers
        )
        assert otra.status_code == 409, otra.text
        assert otra.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REUSED"

        # Y la segunda NO se guardó: el rechazo es explícito, no silencioso.
        ventas = (await client.get("/api/v1/sales", headers=auth_headers)).json()
        assert len([v for v in ventas if v["product_id"] == pid]) == 1

    async def test_un_precio_distinto_tambien_es_otra_operacion(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """La huella cubre el contenido comercial completo, no solo las cantidades."""
        pid = await _producto(client, auth_headers, "Vino", 20)
        headers = {**auth_headers, "Idempotency-Key": "ticket-004"}

        await client.post(
            "/api/v1/sales/manual-batch",
            json=_carrito(pid, unit_price="1000.00"),
            headers=headers,
        )
        otra = await client.post(
            "/api/v1/sales/manual-batch",
            json=_carrito(pid, unit_price="900.00"),
            headers=headers,
        )
        assert otra.status_code == 409
        assert otra.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REUSED"

    async def test_sin_clave_no_hay_idempotencia(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """La cabecera es opcional: sin ella, dos ventas iguales son dos ventas."""
        pid = await _producto(client, auth_headers, "Agua", 20)
        await client.post("/api/v1/sales/manual-batch", json=_carrito(pid), headers=auth_headers)
        await client.post("/api/v1/sales/manual-batch", json=_carrito(pid), headers=auth_headers)
        ventas = (await client.get("/api/v1/sales", headers=auth_headers)).json()
        assert len([v for v in ventas if v["product_id"] == pid]) == 2


class TestClaimANivelDeServicio:
    """Propiedades que el fixture HTTP no puede observar.

    `conftest.override_session` reemplaza `get_db_session` por un `yield` pelado:
    no commitea ni revierte. O sea que a través del cliente HTTP una petición
    fallida NO revierte, y cualquier test de "el rollback libera la clave" estaría
    midiendo el fixture en vez del comportamiento de producción.
    """

    async def test_el_rollback_libera_la_clave(self, db_session, sample_tenant) -> None:
        """Si la venta no entra, la clave no queda reclamada.

        Importa para una caja: un rechazo por stock quemaría la clave y el reintento
        legítimo —después de reponer— chocaría para siempre contra un fantasma. El
        claim comparte transacción con la venta justamente para esto.
        """
        from app.application.services.idempotency import (
            Reclamada,
            claim_idempotent_request,
        )

        tid = sample_tenant.tenant_id
        payload = {"items": [{"quantity": 2}]}

        primero = await claim_idempotent_request(db_session, tid, "k-roll", "ACC", payload)
        assert isinstance(primero, Reclamada)

        await db_session.rollback()

        segundo = await claim_idempotent_request(db_session, tid, "k-roll", "ACC", payload)
        assert isinstance(segundo, Reclamada), "el rollback tiene que haber soltado la clave"

    async def test_sin_respuesta_registrada_el_replay_degrada(
        self, db_session, sample_tenant
    ) -> None:
        """Una ruta que reclama y no registra resultado no puede devolver nada.

        El replay lo dice (`respuesta is None`) en vez de fabricar un cuerpo: el
        caller degrada al 409 de siempre, que es información correcta aunque pobre.
        """
        from app.application.services.idempotency import (
            Reclamada,
            Repeticion,
            claim_idempotent_request,
        )

        tid = sample_tenant.tenant_id
        payload = {"items": [{"quantity": 2}]}

        assert isinstance(
            await claim_idempotent_request(db_session, tid, "k-sin", "ACC", payload), Reclamada
        )
        await db_session.flush()

        repeticion = await claim_idempotent_request(db_session, tid, "k-sin", "ACC", payload)
        assert isinstance(repeticion, Repeticion)
        assert repeticion.respuesta is None

    async def test_la_huella_ignora_el_orden_de_las_claves(self) -> None:
        """Dos formas de escribir el MISMO pedido no pueden dar un conflicto falso."""
        from app.application.services.idempotency import hash_peticion

        assert hash_peticion({"a": 1, "b": {"x": 1, "y": 2}}) == hash_peticion(
            {"b": {"y": 2, "x": 1}, "a": 1}
        )

    async def test_la_huella_distingue_contenido_distinto(self) -> None:
        from app.application.services.idempotency import hash_peticion

        assert hash_peticion({"quantity": 2}) != hash_peticion({"quantity": 3})
