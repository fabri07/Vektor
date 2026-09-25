"""Compuertas HTTP del escaneo estricto y el aprendizaje de códigos (B4).

El riesgo que cubren es uno solo, en dos formas: que la caja cobre el producto
equivocado. Por elegir entre candidatos (lookup) o por fabricar una colisión que
después hay que elegir (aprendizaje).
"""

import unittest.mock
import uuid
from typing import Any

import pytest
from httpx import AsyncClient

from app.domain.business_time import today_ar
from app.domain.scan_code import digito_verificador_gs1


def _ean13(cuerpo12: str) -> str:
    return cuerpo12 + str(digito_verificador_gs1(cuerpo12))


EAN_A = _ean13("779004000012")
EAN_B = _ean13("779004000029")


async def _producto(client: AsyncClient, headers: dict[str, Any], name: str, **extra: Any) -> dict:
    resp = await client.post(
        "/api/v1/products",
        json={"name": name, "sale_price_ars": "100.00", "stock_units": 10, **extra},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def _lookup(client: AsyncClient, headers: dict[str, Any], code: str) -> Any:
    return await client.get("/api/v1/products/lookup", params={"code": code}, headers=headers)


class TestLookup:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
            yield

    async def test_ean_cargado_resuelve(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        p = await _producto(client, auth_headers, "Coca 500", barcode=EAN_A)
        resp = await _lookup(client, auth_headers, EAN_A + "\r")
        assert resp.status_code == 200, resp.text
        assert resp.json()["product"]["id"] == p["id"]
        assert resp.json()["matched_by"] == "barcode"
        assert resp.json()["code_type"] == "GTIN"

    async def test_upc_a_encuentra_el_ean13_guardado(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """El lector puede mandar 12 dígitos para un EAN-13 que empieza en 0."""
        upc = "036000291452"
        p = await _producto(client, auth_headers, "Importado", barcode="0" + upc)
        resp = await _lookup(client, auth_headers, upc)
        assert resp.status_code == 200, resp.text
        assert resp.json()["product"]["id"] == p["id"]

    async def test_sku_interno_resuelve(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        p = await _producto(client, auth_headers, "Florero")
        resp = await _lookup(client, auth_headers, p["internal_sku"])
        assert resp.status_code == 200, resp.text
        assert resp.json()["matched_by"] == "internal_sku"

    async def test_opaco_busca_el_sku_literal(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        p = await _producto(client, auth_headers, "Balde", sku="LIMP-0042")
        resp = await _lookup(client, auth_headers, "limp-0042")
        assert resp.status_code == 200, resp.text
        assert resp.json()["product"]["id"] == p["id"]

    async def test_alfanumerico_no_extrae_digitos(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """`AB-<ean8>` no puede resolver al producto que tiene ese EAN-8."""
        ean8 = "96385074"
        await _producto(client, auth_headers, "Chicle", barcode=ean8)
        resp = await _lookup(client, auth_headers, f"AB-{ean8}")
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"]["code"] == "SCAN_NOT_FOUND"

    async def test_gtin_de_a_que_es_el_sku_de_b_es_ambiguo(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """El caso del plan: nunca elegir."""
        a = await _producto(client, auth_headers, "Yerba A", barcode=EAN_A)
        b = await _producto(client, auth_headers, "Yerba B", sku=EAN_A)
        resp = await _lookup(client, auth_headers, EAN_A)
        assert resp.status_code == 409, resp.text
        detalle = resp.json()["detail"]
        assert detalle["code"] == "SCAN_AMBIGUOUS"
        ids = {c["product_id"] for c in detalle["candidates"]}
        assert ids == {a["id"], b["id"]}
        # B13: cada candidato trae la vista de caja, para que la caja tenga el
        # precio después de que el cajero elija — y sin costos (B5).
        for candidato in detalle["candidates"]:
            assert candidato["product"]["id"] == candidato["product_id"]
            assert "sale_price_ars" in candidato["product"]
            assert "unit_cost_ars" not in candidato["product"]
            assert "margin_pct" not in candidato["product"]

    async def test_desconocido_se_puede_aprender(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        resp = await _lookup(client, auth_headers, EAN_B)
        assert resp.status_code == 404
        assert resp.json()["detail"]["learnable"] is True

    async def test_balanza_sin_producto_no_se_lee(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        resp = await _lookup(client, auth_headers, _ean13("201234500150"))
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "SCAN_SCALE_NOT_SUPPORTED"

    async def test_producto_inactivo_no_aparece(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        p = await _producto(client, auth_headers, "Viejo", barcode=EAN_A)
        await client.patch(
            f"/api/v1/products/{p['id']}", json={"is_active": False}, headers=auth_headers
        )
        assert (await _lookup(client, auth_headers, EAN_A)).status_code == 404

    async def test_otro_tenant_no_ve_mis_codigos(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        second_auth_headers: dict[str, Any],
    ) -> None:
        await _producto(client, auth_headers, "Coca", barcode=EAN_A)
        assert (await _lookup(client, second_auth_headers, EAN_A)).status_code == 404


class TestAprendizaje:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
            yield

    async def _aprender(
        self, client: AsyncClient, headers: dict[str, Any], product_id: str, code: str
    ) -> Any:
        return await client.post(
            f"/api/v1/products/{product_id}/barcode", json={"barcode": code}, headers=headers
        )

    async def test_aprender_y_el_segundo_scan_resuelve(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        p = await _producto(client, auth_headers, "Alfajor")
        resp = await self._aprender(client, auth_headers, p["id"], EAN_A + "\r")
        assert resp.status_code == 200, resp.text
        assert resp.json()["barcode"] == EAN_A
        scan = await _lookup(client, auth_headers, EAN_A)
        assert scan.status_code == 200
        assert scan.json()["product"]["id"] == p["id"]

    async def test_aprender_dos_veces_lo_mismo_es_idempotente(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        p = await _producto(client, auth_headers, "Alfajor")
        assert (await self._aprender(client, auth_headers, p["id"], EAN_A)).status_code == 200
        otra = await self._aprender(client, auth_headers, p["id"], EAN_A)
        assert otra.status_code == 200
        assert otra.json()["barcode"] == EAN_A

    async def test_nunca_pisa_un_codigo_existente(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        p = await _producto(client, auth_headers, "Alfajor", barcode=EAN_A)
        resp = await self._aprender(client, auth_headers, p["id"], EAN_B)
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["code"] == "BARCODE_ALREADY_SET"
        prod = await client.get(f"/api/v1/products/{p['id']}", headers=auth_headers)
        assert prod.json()["barcode"] == EAN_A

    async def test_nunca_pisa_un_codigo_alfanumerico(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """Un código sin dígitos tiene barcode_normalized None: igual cuenta."""
        p = await _producto(client, auth_headers, "Mantel", barcode="DECO-ABC")
        resp = await self._aprender(client, auth_headers, p["id"], EAN_B)
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["code"] == "BARCODE_ALREADY_SET"

    async def test_codigo_de_otro_producto_va_a_revision(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """Otra caja ya lo vinculó: 409 y las ventas anteriores intactas."""
        a = await _producto(client, auth_headers, "Coca A", barcode=EAN_A)
        b = await _producto(client, auth_headers, "Coca B")
        venta = await client.post(
            "/api/v1/pos/operations",
            json={
                "client_operation_id": f"op-{uuid.uuid4().hex[:16]}",
                "operation_date": f"{today_ar().isoformat()}T10:00:00",
                "items": [{"product_id": b["id"], "quantity": 1}],
                "tenders": [{"payment_method": "cash", "amount_ars": "100.00"}],
            },
            headers=auth_headers,
        )
        assert venta.status_code == 201, venta.text

        resp = await self._aprender(client, auth_headers, b["id"], EAN_A)
        assert resp.status_code == 409, resp.text
        detalle = resp.json()["detail"]
        assert detalle["code"] == "BARCODE_TAKEN"
        assert detalle["existing_id"] == a["id"]

        sale_id = venta.json()["lines"][0]["sale_entry_id"]
        guardada = await client.get(f"/api/v1/sales/{sale_id}", headers=auth_headers)
        assert guardada.json()["product_id"] == b["id"]
        prod_b = await client.get(f"/api/v1/products/{b['id']}", headers=auth_headers)
        assert prod_b.json()["barcode"] is None

    async def test_no_fabrica_la_ambiguedad_con_un_sku(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """Vincular el EAN que ya es el SKU de otro produciría un SCAN_AMBIGUOUS."""
        a = await _producto(client, auth_headers, "Con SKU", sku=EAN_A)
        b = await _producto(client, auth_headers, "Sin nada")
        resp = await self._aprender(client, auth_headers, b["id"], EAN_A)
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["existing_id"] == a["id"]
        assert resp.json()["detail"]["field"] == "sku"

    @pytest.mark.parametrize(
        "codigo",
        ["LIMP-0042", "VKT-0123456789AB", _ean13("201234500150"), EAN_A[:-1] + "0"],
    )
    async def test_solo_se_aprende_un_gtin_valido(
        self, client: AsyncClient, auth_headers: dict[str, Any], codigo: str
    ) -> None:
        if codigo == EAN_A[:-1] + "0" and EAN_A[-1] == "0":
            pytest.skip("el verificador real es 0; la variante corrupta no aplica")
        p = await _producto(client, auth_headers, "Algo")
        resp = await self._aprender(client, auth_headers, p["id"], codigo)
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["code"] == "SCAN_CODE_NOT_LEARNABLE"

    async def test_un_viewer_no_aprende(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        viewer_headers: dict[str, Any],
    ) -> None:
        p = await _producto(client, auth_headers, "Alfajor")
        resp = await self._aprender(client, viewer_headers, p["id"], EAN_A)
        assert resp.status_code == 403
