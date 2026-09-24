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


async def _cobrar(
    client: AsyncClient, headers: dict[str, Any], productos: list[tuple[str, int]], total: str
) -> dict[str, Any]:
    cuerpo = _operacion(
        [{"product_id": p, "quantity": q} for p, q in productos],
        [{"payment_method": "cash", "amount_ars": total}],
    )
    resp = await client.post("/api/v1/pos/operations", json=cuerpo, headers=headers)
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def _stock(client: AsyncClient, headers: dict[str, Any], product_id: str) -> int:
    resp = await client.get(f"/api/v1/products/{product_id}", headers=headers)
    return int(resp.json()["stock_units"])


class TestAnulacionDeTicket:
    """B10. Se anula el ticket entero, nunca una línea suelta."""

    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
            yield

    async def test_anular_revierte_todas_las_lineas_y_el_stock(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        p1 = await _crear_producto(client, auth_headers, "Yerba", stock=10, price="1000.00")
        p2 = await _crear_producto(client, auth_headers, "Fideos", stock=5, price="500.00")
        op = await _cobrar(client, auth_headers, [(p1, 2), (p2, 3)], "3500.00")
        assert await _stock(client, auth_headers, p1) == 8
        assert await _stock(client, auth_headers, p2) == 2

        resp = await client.post(
            f"/api/v1/pos/operations/{op['id']}/void",
            json={"reason": "el cliente se arrepintió"},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "VOIDED"
        # Los pagos se conservan: el ticket anulado se reimprime y se audita.
        assert len(resp.json()["tenders"]) == 1

        assert await _stock(client, auth_headers, p1) == 10
        assert await _stock(client, auth_headers, p2) == 5
        for linea in op["lines"]:
            venta = await client.get(
                f"/api/v1/sales/{linea['sale_entry_id']}", headers=auth_headers
            )
            assert venta.status_code == 404, "la venta tendría que estar anulada"

    async def test_anular_dos_veces_no_repone_stock_dos_veces(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        p1 = await _crear_producto(client, auth_headers, "Yerba", stock=10, price="1000.00")
        op = await _cobrar(client, auth_headers, [(p1, 4)], "4000.00")
        url = f"/api/v1/pos/operations/{op['id']}/void"

        primera = await client.post(url, headers=auth_headers)
        segunda = await client.post(url, headers=auth_headers)
        assert primera.status_code == segunda.status_code == 200
        assert segunda.json()["status"] == "VOIDED"
        assert await _stock(client, auth_headers, p1) == 10

    async def test_deja_auditoria_de_la_operacion(
        self, client: AsyncClient, auth_headers: dict[str, Any], db_session: Any
    ) -> None:
        from sqlalchemy import select

        from app.persistence.models.audit import DecisionAuditLog

        p1 = await _crear_producto(client, auth_headers, "Yerba", stock=10, price="1000.00")
        op = await _cobrar(client, auth_headers, [(p1, 1)], "1000.00")
        await client.post(
            f"/api/v1/pos/operations/{op['id']}/void",
            json={"reason": "error de carga"},
            headers=auth_headers,
        )
        await client.post(f"/api/v1/pos/operations/{op['id']}/void", headers=auth_headers)

        filas = (
            await db_session.execute(
                select(DecisionAuditLog).where(
                    DecisionAuditLog.decision_type == "POS_OPERATION_VOIDED"
                )
            )
        ).scalars().all()
        # Una sola: el segundo void no hizo nada y no lo registra como si hubiera hecho.
        assert len(filas) == 1
        datos = filas[0].decision_data
        assert datos["record_id"] == op["id"]
        assert datos["reason"] == "error de carga"
        assert datos["sale_entry_ids"] == [op["lines"][0]["sale_entry_id"]]

    async def test_otro_tenant_no_puede_anular(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        second_auth_headers: dict[str, Any],
    ) -> None:
        p1 = await _crear_producto(client, auth_headers, "Yerba", stock=10, price="1000.00")
        op = await _cobrar(client, auth_headers, [(p1, 1)], "1000.00")
        resp = await client.post(
            f"/api/v1/pos/operations/{op['id']}/void", headers=second_auth_headers
        )
        assert resp.status_code == 404
        assert await _stock(client, auth_headers, p1) == 9

    async def test_un_viewer_no_puede_anular(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        viewer_headers: dict[str, Any],
    ) -> None:
        p1 = await _crear_producto(client, auth_headers, "Yerba", stock=10, price="1000.00")
        op = await _cobrar(client, auth_headers, [(p1, 1)], "1000.00")
        resp = await client.post(f"/api/v1/pos/operations/{op['id']}/void", headers=viewer_headers)
        assert resp.status_code == 403
        assert await _stock(client, auth_headers, p1) == 9


class TestLineaDeCajaNoSeTocaSuelta:
    """El PATCH/DELETE genérico no puede dejar un ticket a medio anular."""

    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
            yield

    async def test_delete_de_una_linea_de_caja_da_409(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        p1 = await _crear_producto(client, auth_headers, "Yerba", stock=10, price="1000.00")
        op = await _cobrar(client, auth_headers, [(p1, 2)], "2000.00")
        venta_id = op["lines"][0]["sale_entry_id"]

        resp = await client.delete(f"/api/v1/sales/{venta_id}", headers=auth_headers)
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["code"] == "BELONGS_TO_POS_OPERATION"
        assert resp.json()["detail"]["operation_id"] == op["id"]
        # Nada cambió: la venta sigue viva y el stock no se repuso.
        assert (
            await client.get(f"/api/v1/sales/{venta_id}", headers=auth_headers)
        ).status_code == 200
        assert await _stock(client, auth_headers, p1) == 8

    async def test_patch_de_una_linea_de_caja_da_409(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        p1 = await _crear_producto(client, auth_headers, "Yerba", stock=10, price="1000.00")
        op = await _cobrar(client, auth_headers, [(p1, 2)], "2000.00")
        venta_id = op["lines"][0]["sale_entry_id"]

        resp = await client.patch(
            f"/api/v1/sales/{venta_id}", json={"amount": "1.00"}, headers=auth_headers
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["code"] == "BELONGS_TO_POS_OPERATION"
        venta = await client.get(f"/api/v1/sales/{venta_id}", headers=auth_headers)
        assert float(venta.json()["amount"]) == 2000.0

    async def test_una_venta_comun_se_sigue_borrando(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """El guard no puede alcanzar a lo que no es caja."""
        resp = await client.post(
            "/api/v1/sales",
            json={"amount": "500.00", "transaction_date": _FECHA, "payment_method": "cash"},
            headers=auth_headers,
        )
        assert resp.status_code == 201, resp.text
        borrado = await client.delete(f"/api/v1/sales/{resp.json()['id']}", headers=auth_headers)
        assert borrado.status_code == 200, borrado.text
