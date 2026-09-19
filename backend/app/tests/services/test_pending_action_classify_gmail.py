"""Tests de CLASSIFY_GMAIL_MESSAGE en PendingActionService.

Cubre el cableado real: execute_pending_action -> review_gmail_inbox ->
preflight, con auditoría de los mensajes filtrados y el resultado
persistido en action.payload["result"] para que el endpoint de confirm lo
devuelva al frontend.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.application.agents.shared.schemas import ActionType
from app.application.services.pending_action_service import execute_pending_action
from app.integrations.mcp.google_mcp_service import GmailListPage

_PATCH_BROKER = "app.application.services.pending_action_service._make_google_broker"
_PATCH_SENDERS = "app.application.services.gmail_inbox_review_service.get_approved_senders"


@pytest.fixture(autouse=True)
def _suscripcion_habilitada(monkeypatch: pytest.MonkeyPatch) -> None:
    """Estos tests pasan un `db` de mentira y prueban el broker, no la
    suscripción: el control del embudo (`execute_pending_action`) consulta la
    base de verdad, y está probado en `test_subscription_write_gate.py`."""
    monkeypatch.setattr(
        "app.application.services.pending_action_service."
        "subscription_service.assert_tenant_can_write",
        AsyncMock(),
    )



def _make_action(payload: dict[str, Any]):
    from app.persistence.models.pending_action import PendingAction  # noqa: PLC0415

    action = PendingAction()
    action.id = uuid.uuid4()
    action.tenant_id = uuid.uuid4()
    action.user_id = uuid.uuid4()
    action.action_type = ActionType.CLASSIFY_GMAIL_MESSAGE
    action.payload = payload
    action.risk_level = "LOW"
    action.status = "APPROVED"
    action.external_system = "GOOGLE_GMAIL"
    return action


def _make_broker(
    *, message_ids: list[str], metadata: dict[str, dict], full: dict[str, dict]
) -> AsyncMock:
    broker = AsyncMock()
    broker.list_gmail_messages = AsyncMock(
        return_value=GmailListPage(message_ids=message_ids, next_page_token=None)
    )
    broker.list_gmail_labels = AsyncMock(return_value=[])

    async def _get(message_id: str, *, msg_format: str = "full"):
        return (metadata if msg_format == "metadata" else full)[message_id]

    broker.get_gmail_message = AsyncMock(side_effect=_get)
    return broker


async def test_classify_gmail_message_stashes_result_and_audits_skips():
    action = _make_action({"mode": "mcp", "query": "in:inbox", "max_results": 10})
    broker = _make_broker(
        message_ids=["approved", "spam"],
        metadata={
            "approved": {"from": "Prov <p@ok.com>", "subject": "Factura", "label_ids": []},
            "spam": {"from": "x@spam.com", "subject": "Oferta", "label_ids": []},
        },
        full={"approved": {"from": "Prov <p@ok.com>", "subject": "Factura", "date": "hoy"}},
    )
    db = MagicMock()
    db.add = MagicMock()

    with (
        patch(_PATCH_BROKER, return_value=broker),
        patch(_PATCH_SENDERS, new=AsyncMock(return_value=["p@ok.com"])),
    ):
        await execute_pending_action(action, db)

    result = action.payload["result"]
    assert result["authorized_count"] == 1
    assert result["authorized"][0]["message_id"] == "approved"
    assert result["skipped_count"] == 1
    assert result["skipped"][0]["message_id"] == "spam"

    # Un GMAIL_SKIPPED auditado por cada mensaje filtrado (invariante 6).
    audit_rows = [call.args[0] for call in db.add.call_args_list]
    skipped_audits = [row for row in audit_rows if row.decision_type == "GMAIL_SKIPPED"]
    assert len(skipped_audits) == 1
    assert skipped_audits[0].decision_data["sender"] == "x@spam.com"
    assert skipped_audits[0].tenant_id == action.tenant_id


async def test_classify_gmail_message_explicit_id_does_not_call_list():
    action = _make_action({"mode": "mcp", "message_id": "m1"})
    broker = _make_broker(
        message_ids=[],
        metadata={"m1": {"from": "cualquiera@x.com", "subject": "X", "label_ids": []}},
        full={"m1": {"from": "cualquiera@x.com", "subject": "X", "date": "hoy"}},
    )
    db = MagicMock()

    with (
        patch(_PATCH_BROKER, return_value=broker),
        patch(_PATCH_SENDERS, new=AsyncMock(return_value=[])),
    ):
        await execute_pending_action(action, db)

    broker.list_gmail_messages.assert_not_called()
    assert action.payload["result"]["authorized_count"] == 1


async def test_classify_gmail_message_invalid_payload_raises():
    action = _make_action({"mode": "mcp"})  # sin query ni message_id
    broker = _make_broker(message_ids=[], metadata={}, full={})
    db = MagicMock()

    with patch(_PATCH_BROKER, return_value=broker):
        try:
            await execute_pending_action(action, db)
        except ValueError as exc:
            assert "query" in str(exc) or "message_id" in str(exc)
        else:
            raise AssertionError("esperaba ValueError por payload sin query ni message_id")


async def test_classify_gmail_message_non_mcp_mode_skips_broker_entirely():
    action = _make_action({"mode": "informational"})
    db = MagicMock()

    with patch(_PATCH_BROKER) as make_broker:
        await execute_pending_action(action, db)
        make_broker.assert_not_called()

    assert "result" not in (action.payload or {})
