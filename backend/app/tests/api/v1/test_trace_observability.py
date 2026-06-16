"""Tests del backbone de trazabilidad: trace_id transversal + external_operation_logs.

- trace_id del header X-Trace-Id se autocaptura en decision_audit_log.
- envío de email registra un ExternalOperationLog (resend) + provider_message_id.
- fallo de envío → log failed.
- redacción de secretos en el payload.
"""

import uuid
from typing import Any
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.application.services.external_operation_service import record_external_operation
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.communication_log import CommunicationLog
from app.persistence.models.external_operation_log import ExternalOperationLog


@pytest.mark.asyncio
class TestTraceId:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_trace_id_propagates_to_audit(
        self, client: AsyncClient, auth_headers: dict[str, Any], db_session
    ) -> None:
        tid = str(uuid.uuid4())
        resp = await client.post(
            "/api/v1/customers",
            json={"name": "Traza"},
            headers={**auth_headers, "X-Trace-Id": tid},
        )
        assert resp.status_code == 201
        assert resp.headers.get("X-Trace-Id") == tid

        rows = (
            await db_session.execute(
                select(DecisionAuditLog).where(DecisionAuditLog.trace_id == tid)
            )
        ).scalars().all()
        assert len(rows) >= 1


@pytest.mark.asyncio
class TestCommunicationExternalOps:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def _make_customer(self, client: AsyncClient, headers: dict[str, Any]) -> str:
        resp = await client.post(
            "/api/v1/customers",
            json={"name": "Cli", "email": "cli@x.com"},
            headers=headers,
        )
        assert resp.status_code == 201
        return resp.json()["id"]

    async def test_email_send_records_external_operation(
        self, client: AsyncClient, auth_headers: dict[str, Any], db_session
    ) -> None:
        cid = await self._make_customer(client, auth_headers)
        with patch(
            "app.integrations.communication.email_channel.SMTPClient.send",
            return_value="resend-id-123",
        ):
            resp = await client.post(
                f"/api/v1/communication/customers/{cid}/send",
                json={"channel": "email", "subject": "Hola", "body": "Mensaje"},
                headers=auth_headers,
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "sent"

        ext = (
            await db_session.execute(
                select(ExternalOperationLog).where(
                    ExternalOperationLog.operation_type == "email_send"
                )
            )
        ).scalars().all()
        assert len(ext) == 1
        assert ext[0].provider == "resend"
        assert ext[0].status == "success"
        assert ext[0].provider_request_id == "resend-id-123"
        assert ext[0].trace_id is not None

        comm = (
            await db_session.execute(select(CommunicationLog))
        ).scalars().all()
        assert comm[0].provider_message_id == "resend-id-123"

    async def test_failed_email_records_failed(
        self, client: AsyncClient, auth_headers: dict[str, Any], db_session
    ) -> None:
        cid = await self._make_customer(client, auth_headers)
        with patch(
            "app.integrations.communication.email_channel.SMTPClient.send",
            side_effect=RuntimeError("resend down"),
        ):
            resp = await client.post(
                f"/api/v1/communication/customers/{cid}/send",
                json={"channel": "email", "body": "Mensaje"},
                headers=auth_headers,
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "failed"

        ext = (
            await db_session.execute(
                select(ExternalOperationLog).where(
                    ExternalOperationLog.status == "failed"
                )
            )
        ).scalars().all()
        assert len(ext) == 1
        assert "resend down" in (ext[0].error_message or "")


@pytest.mark.asyncio
async def test_record_external_operation_redacts_secrets(db_session, sample_tenant) -> None:
    log = await record_external_operation(
        db_session,
        tenant_id=sample_tenant.tenant_id,
        operation_type="email_send",
        provider="resend",
        status="success",
        request_payload={"api_key": "supersecret", "to": "x@y.com", "nested": {"token": "t"}},
    )
    assert log.request_payload_redacted == {
        "api_key": "***",
        "to": "x@y.com",
        "nested": {"token": "***"},
    }
