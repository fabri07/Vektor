"""Compuertas HTTP de la operación de caja (B3).

Lo que se protege acá y no en el dominio: que el servidor ponga los precios,
que la identidad de operación sea recuperable, y que una venta no quede a
medias entre `sales_entries` y `pos_operations`.
"""

import unittest.mock
import uuid
from typing import Any

import pytest
from httpx import AsyncClient

from app.domain.business_time import today_ar

_FECHA = f"{today_ar().isoformat()}T10:00:00"


async def _crear_producto(
    client: AsyncClient,
    headers: dict[str, Any],
    name: str,
    *,
    stock: int = 10,
    price: str = "100.00",
) -> str:
    resp = await client.post(
        "/api/v1/products",
        json={"name": name, "sale_price_ars": price, "stock_units": stock},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def _operacion(items: list[dict[str, Any]], tenders: list[dict[str, Any]], **extra: Any) -> dict:
    return {
        "client_operation_id": extra.pop("client_operation_id", f"op-{uuid.uuid4().hex[:16]}"),
        "operation_date": _FECHA,
        "items": items,
        "tenders": tenders,
        **extra,
    }


class TestOperacionDeCaja:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
            yield

    async def test_cobra_el_carrito_y_descuenta_stock(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        p1 = await _crear_producto(client, auth_headers, "Yerba", stock=10, price="1000.00")
        p2 = await _crear_producto(client, auth_headers, "Fideos", stock=5, price="500.00")
        cuerpo = _operacion(
            [{"product_id": p1, "quantity": 2}, {"product_id": p2, "quantity": 1}],
            [{"payment_method": "cash", "amount_ars": "2500.00"}],
        )
        resp = await client.post("/api/v1/pos/operations", json=cuerpo, headers=auth_headers)
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["total_ars"] == "2500.00"
        assert len(data["lines"]) == 2
        assert len(data["tenders"]) == 1
        # Cada línea apunta a la venta contable que produjo.
        assert all(linea["sale_entry_id"] for linea in data["lines"])

        prod = await client.get(f"/api/v1/products/{p1}", headers=auth_headers)
        assert prod.json()["stock_units"] == 8

    async def test_el_precio_lo_pone_el_catalogo(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """El cuerpo no tiene dónde mandar un precio: es la defensa estructural."""
        p1 = await _crear_producto(client, auth_headers, "Yerba", price="1000.00")
        cuerpo = _operacion(
            [{"product_id": p1, "quantity": 1, "unit_price_list": "1.00"}],
            [{"payment_method": "cash", "amount_ars": "1000.00"}],
        )
        resp = await client.post("/api/v1/pos/operations", json=cuerpo, headers=auth_headers)
        assert resp.status_code == 201, resp.text
        assert resp.json()["lines"][0]["unit_price_list"] == "1000.00"

    async def test_el_descuento_indivisible_cierra(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """$1 sobre tres unidades de $100, de punta a punta."""
        p1 = await _crear_producto(client, auth_headers, "Alfajor", stock=10, price="100.00")
        cuerpo = _operacion(
            [{"product_id": p1, "quantity": 3}],
            [{"payment_method": "cash", "amount_ars": "299.00"}],
            discount_ars="1.00",
        )
        resp = await client.post("/api/v1/pos/operations", json=cuerpo, headers=auth_headers)
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["subtotal_ars"] == "300.00"
        assert data["total_ars"] == "299.00"
        suma = sum(float(linea["line_total_ars"]) for linea in data["lines"])
        assert suma == float(data["total_ars"])

    async def test_los_pagos_tienen_que_sumar_la_venta(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        p1 = await _crear_producto(client, auth_headers, "Yerba", price="1000.00")
        cuerpo = _operacion(
            [{"product_id": p1, "quantity": 1}],
            [{"payment_method": "cash", "amount_ars": "999.99"}],
        )
        resp = await client.post("/api/v1/pos/operations", json=cuerpo, headers=auth_headers)
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["code"] == "TENDERS_MISMATCH"

    async def test_el_vuelto_no_infla_la_venta(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        p1 = await _crear_producto(client, auth_headers, "Yerba", price="8500.00")
        cuerpo = _operacion(
            [{"product_id": p1, "quantity": 1}],
            [{"payment_method": "cash", "amount_ars": "8500.00"}],
            cash_received_ars="10000.00",
        )
        resp = await client.post("/api/v1/pos/operations", json=cuerpo, headers=auth_headers)
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["total_ars"] == "8500.00"
        assert data["cash_change_ars"] == "1500.00"

    async def test_entregar_menos_que_el_total_se_rechaza(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        p1 = await _crear_producto(client, auth_headers, "Yerba", price="8500.00")
        cuerpo = _operacion(
            [{"product_id": p1, "quantity": 1}],
            [{"payment_method": "cash", "amount_ars": "8500.00"}],
            cash_received_ars="8000.00",
        )
        resp = await client.post("/api/v1/pos/operations", json=cuerpo, headers=auth_headers)
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["code"] == "CASH_RECEIVED_TOO_LOW"

    async def test_stock_insuficiente_rechaza_con_codigo(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        p1 = await _crear_producto(client, auth_headers, "Yerba", stock=1, price="100.00")
        cuerpo = _operacion(
            [{"product_id": p1, "quantity": 5}],
            [{"payment_method": "cash", "amount_ars": "500.00"}],
        )
        resp = await client.post("/api/v1/pos/operations", json=cuerpo, headers=auth_headers)
        assert resp.status_code == 400, resp.text
        assert resp.json()["code"] == "INSUFFICIENT_STOCK"

    async def test_pago_mixto_todavia_no(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """Hasta que B9 cablee los ocho lectores, un mixto no se puede imputar."""
        p1 = await _crear_producto(client, auth_headers, "Yerba", price="1000.00")
        cuerpo = _operacion(
            [{"product_id": p1, "quantity": 1}],
            [
                {"payment_method": "cash", "amount_ars": "600.00"},
                {"payment_method": "debit_card", "amount_ars": "400.00"},
            ],
        )
        resp = await client.post("/api/v1/pos/operations", json=cuerpo, headers=auth_headers)
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["code"] == "MIXED_TENDER_NOT_ENABLED"

    async def test_fecha_futura_se_rechaza(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        p1 = await _crear_producto(client, auth_headers, "Yerba", price="1000.00")
        cuerpo = _operacion(
            [{"product_id": p1, "quantity": 1}],
            [{"payment_method": "cash", "amount_ars": "1000.00"}],
        )
        cuerpo["operation_date"] = "2099-01-01T10:00:00"
        resp = await client.post("/api/v1/pos/operations", json=cuerpo, headers=auth_headers)
        assert resp.status_code == 422

    async def test_producto_repetido_se_rechaza(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        p1 = await _crear_producto(client, auth_headers, "Yerba", stock=10, price="100.00")
        cuerpo = _operacion(
            [{"product_id": p1, "quantity": 1}, {"product_id": p1, "quantity": 2}],
            [{"payment_method": "cash", "amount_ars": "300.00"}],
        )
        resp = await client.post("/api/v1/pos/operations", json=cuerpo, headers=auth_headers)
        assert resp.status_code == 400


class TestIdentidadDeOperacion:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
            yield

    async def test_el_reintento_devuelve_el_mismo_ticket_y_no_cobra_de_nuevo(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """El caso que produce el doble cobro: la respuesta se perdió."""
        p1 = await _crear_producto(client, auth_headers, "Yerba", stock=10, price="1000.00")
        cuerpo = _operacion(
            [{"product_id": p1, "quantity": 1}],
            [{"payment_method": "cash", "amount_ars": "1000.00"}],
        )
        primera = await client.post("/api/v1/pos/operations", json=cuerpo, headers=auth_headers)
        assert primera.status_code == 201, primera.text

        segunda = await client.post("/api/v1/pos/operations", json=cuerpo, headers=auth_headers)
        # 200 y no 201: el ticket ya existía.
        assert segunda.status_code == 200, segunda.text
        assert segunda.json()["id"] == primera.json()["id"]

        # Y el stock se descontó UNA sola vez.
        prod = await client.get(f"/api/v1/products/{p1}", headers=auth_headers)
        assert prod.json()["stock_units"] == 9

    async def test_el_mismo_id_con_otro_contenido_se_rechaza(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """Una clave reusada no es un reintento: tragarla borraría la 2ª venta."""
        p1 = await _crear_producto(client, auth_headers, "Yerba", stock=10, price="1000.00")
        ident = f"op-{uuid.uuid4().hex[:16]}"
        cuerpo = _operacion(
            [{"product_id": p1, "quantity": 1}],
            [{"payment_method": "cash", "amount_ars": "1000.00"}],
            client_operation_id=ident,
        )
        assert (
            await client.post("/api/v1/pos/operations", json=cuerpo, headers=auth_headers)
        ).status_code == 201

        otro = _operacion(
            [{"product_id": p1, "quantity": 2}],
            [{"payment_method": "cash", "amount_ars": "2000.00"}],
            client_operation_id=ident,
        )
        resp = await client.post("/api/v1/pos/operations", json=otro, headers=auth_headers)
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REUSED"


class TestTrazabilidadDelCajero:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
            yield

    async def test_la_operacion_guarda_quien_cobro(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        p1 = await _crear_producto(client, auth_headers, "Yerba", price="1000.00")
        cuerpo = _operacion(
            [{"product_id": p1, "quantity": 1}],
            [{"payment_method": "cash", "amount_ars": "1000.00"}],
        )
        resp = await client.post("/api/v1/pos/operations", json=cuerpo, headers=auth_headers)
        assert resp.status_code == 201, resp.text
        assert resp.json()["created_by_user_id"] is not None
