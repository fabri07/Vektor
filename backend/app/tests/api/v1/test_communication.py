"""Tests para los endpoints de comunicación (email real + WhatsApp dormido).

Cubre:
- GET /communication/channels → email available, whatsapp segun flag.
- Enviar email a un cliente (mock SMTPClient.send) → 200 + log status=sent.
- Cliente sin email → 400.
- Canal whatsapp no configurado → 503 {"code": "CHANNEL_NOT_CONFIGURED"}.
- Cross-tenant → 400 (el cliente no es del tenant).
- Historial lista lo enviado.
- Envío a proveedor (espejo del cliente).
"""

from typing import Any
from unittest.mock import patch

import pytest
from httpx import AsyncClient


async def _create_customer(
    client: AsyncClient, headers: dict[str, Any], **fields: Any
) -> str:
    payload: dict[str, Any] = {"name": "Cliente Com"}
    payload.update(fields)
    resp = await client.post("/api/v1/customers", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def _create_supplier(
    client: AsyncClient, headers: dict[str, Any], **fields: Any
) -> str:
    payload: dict[str, Any] = {"name": "Proveedor Com"}
    payload.update(fields)
    resp = await client.post("/api/v1/suppliers", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


@pytest.mark.asyncio
class TestCommunication:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_list_channels(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        resp = await client.get("/api/v1/communication/channels", headers=auth_headers)
        assert resp.status_code == 200
        by_channel = {c["channel"]: c["available"] for c in resp.json()}
        assert by_channel["email"] is True
        # WhatsApp dormido por default.
        assert by_channel["whatsapp"] is False

    async def test_send_email_to_customer(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        cid = await _create_customer(client, auth_headers, email="dest@example.com")
        with patch(
            "app.integrations.communication.email_channel.SMTPClient.send"
        ) as mock_send:
            resp = await client.post(
                f"/api/v1/communication/customers/{cid}/send",
                json={"channel": "email", "subject": "Hola", "body": "Mensaje de prueba"},
                headers=auth_headers,
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "sent"
        assert body["channel"] == "email"
        assert body["subject"] == "Hola"
        assert body["error"] is None
        mock_send.assert_called_once()
        kwargs = mock_send.call_args.kwargs
        assert kwargs["to_email"] == "dest@example.com"
        assert kwargs["subject"] == "Hola"

    async def test_send_email_customer_without_email_400(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        cid = await _create_customer(client, auth_headers)  # sin email
        resp = await client.post(
            f"/api/v1/communication/customers/{cid}/send",
            json={"channel": "email", "body": "Hola"},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    async def test_send_whatsapp_not_configured_503(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        cid = await _create_customer(client, auth_headers, phone="+5491100000000")
        resp = await client.post(
            f"/api/v1/communication/customers/{cid}/send",
            json={"channel": "whatsapp", "body": "Hola"},
            headers=auth_headers,
        )
        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "CHANNEL_NOT_CONFIGURED"

    async def test_cross_tenant_cannot_send(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        second_auth_headers: dict[str, Any],
    ) -> None:
        cid = await _create_customer(client, auth_headers, email="dest@example.com")
        # El segundo tenant intenta enviarle al cliente del primero → 400 (no es suyo).
        resp = await client.post(
            f"/api/v1/communication/customers/{cid}/send",
            json={"channel": "email", "body": "Hola"},
            headers=second_auth_headers,
        )
        assert resp.status_code == 400

    async def test_customer_history(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        cid = await _create_customer(client, auth_headers, email="dest@example.com")
        with patch("app.integrations.communication.email_channel.SMTPClient.send"):
            await client.post(
                f"/api/v1/communication/customers/{cid}/send",
                json={"channel": "email", "subject": "A", "body": "primero"},
                headers=auth_headers,
            )
            await client.post(
                f"/api/v1/communication/customers/{cid}/send",
                json={"channel": "email", "subject": "B", "body": "segundo"},
                headers=auth_headers,
            )
        resp = await client.get(
            f"/api/v1/communication/customers/{cid}/history", headers=auth_headers
        )
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) == 2
        assert {r["subject"] for r in rows} == {"A", "B"}
        assert all(r["status"] == "sent" for r in rows)

    async def test_send_email_to_supplier(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        sid = await _create_supplier(client, auth_headers, email="prov@example.com")
        with patch(
            "app.integrations.communication.email_channel.SMTPClient.send"
        ) as mock_send:
            resp = await client.post(
                f"/api/v1/communication/suppliers/{sid}/send",
                json={"channel": "email", "body": "Pedido"},
                headers=auth_headers,
            )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "sent"
        mock_send.assert_called_once()

        hist = await client.get(
            f"/api/v1/communication/suppliers/{sid}/history", headers=auth_headers
        )
        assert hist.status_code == 200
        assert len(hist.json()) == 1
