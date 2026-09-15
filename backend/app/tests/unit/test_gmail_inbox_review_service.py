"""Unit tests — gmail_inbox_review_service.review_gmail_inbox.

Cubre el flujo real (list -> metadata -> preflight -> read) con un broker
mockeado que imita la forma exacta que devuelve el MCP server, para no
repetir el bug histórico de asumir una clave ("messages") que el server
nunca produjo.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.application.services.gmail_inbox_review_service import (
    GmailReviewRequestError,
    review_gmail_inbox,
)
from app.integrations.mcp.exceptions import McpToolAuthError
from app.integrations.mcp.google_mcp_service import GmailListPage

TENANT_ID = uuid.uuid4()


def _broker(
    *,
    message_ids: list[str],
    next_page_token: str | None = None,
    disabled: bool = False,
    metadata_by_id: dict[str, dict] | None = None,
    full_by_id: dict[str, dict] | None = None,
    labels: list[dict] | None = None,
) -> AsyncMock:
    broker = AsyncMock()
    broker.list_gmail_messages = AsyncMock(
        return_value=GmailListPage(
            message_ids=message_ids, next_page_token=next_page_token, disabled=disabled
        )
    )
    broker.list_gmail_labels = AsyncMock(return_value=labels or [])
    metadata_by_id = metadata_by_id or {}
    full_by_id = full_by_id or {}

    async def _get(message_id: str, *, msg_format: str = "full"):
        table = metadata_by_id if msg_format == "metadata" else full_by_id
        return table[message_id]

    broker.get_gmail_message = AsyncMock(side_effect=_get)
    return broker


async def test_no_query_and_no_message_id_raises() -> None:
    broker = _broker(message_ids=[])
    with pytest.raises(GmailReviewRequestError):
        await review_gmail_inbox(
            broker=broker, db=AsyncMock(), tenant_id=TENANT_ID, query=None, message_id=None
        )


async def test_disabled_integration_is_distinguished_from_empty_inbox() -> None:
    broker = _broker(message_ids=[], disabled=True)
    result = await review_gmail_inbox(
        broker=broker, db=AsyncMock(), tenant_id=TENANT_ID, query="in:inbox", message_id=None
    )
    assert result.disabled is True
    assert result.candidates == 0
    broker.get_gmail_message.assert_not_called()


async def test_empty_inbox_without_disabled_flag() -> None:
    broker = _broker(message_ids=[])
    result = await review_gmail_inbox(
        broker=broker, db=AsyncMock(), tenant_id=TENANT_ID, query="in:inbox", message_id=None
    )
    assert result.disabled is False
    assert result.candidates == 0
    assert "no encontré" in result.message.lower()


async def test_approved_sender_with_display_name_is_authorized_and_full_read_happens() -> None:
    sender = "Proveedor S.A. <compras@proveedor.com>"
    broker = _broker(
        message_ids=["m1"],
        metadata_by_id={"m1": {"from": sender, "subject": "Factura", "label_ids": ["INBOX"]}},
        full_by_id={"m1": {"from": sender, "subject": "Factura", "date": "hoy"}},
    )
    with patch(
        "app.application.services.gmail_inbox_review_service.get_approved_senders",
        new=AsyncMock(return_value=["compras@proveedor.com"]),
    ):
        result = await review_gmail_inbox(
            broker=broker, db=AsyncMock(), tenant_id=TENANT_ID, query="in:inbox", message_id=None
        )
    assert result.candidates == 1
    assert len(result.authorized) == 1
    assert result.authorized[0]["message_id"] == "m1"
    assert not result.skipped
    # Se leyó metadata Y contenido completo del autorizado.
    assert broker.get_gmail_message.await_count == 2


async def test_unapproved_sender_is_skipped_without_full_read() -> None:
    meta = {"from": "spam@unknown.com", "subject": "Oferta", "label_ids": ["INBOX"]}
    broker = _broker(message_ids=["m1"], metadata_by_id={"m1": meta})
    with patch(
        "app.application.services.gmail_inbox_review_service.get_approved_senders",
        new=AsyncMock(return_value=[]),
    ):
        result = await review_gmail_inbox(
            broker=broker, db=AsyncMock(), tenant_id=TENANT_ID, query="in:inbox", message_id=None
        )
    assert not result.authorized
    assert len(result.skipped) == 1
    assert result.skipped[0]["message_id"] == "m1"
    # Nunca se pidió el cuerpo completo del mensaje rechazado.
    assert broker.get_gmail_message.await_count == 1  # solo la llamada de metadata


async def test_vektor_label_resolved_via_list_labels_authorizes() -> None:
    sender = "desconocido@x.com"
    broker = _broker(
        message_ids=["m1"],
        metadata_by_id={
            "m1": {"from": sender, "subject": "Pedido", "label_ids": ["INBOX", "Label_999"]}
        },
        full_by_id={"m1": {"from": sender, "subject": "Pedido", "date": "hoy"}},
        labels=[{"id": "Label_999", "name": "Véktor", "type": "user"}],
    )
    with patch(
        "app.application.services.gmail_inbox_review_service.get_approved_senders",
        new=AsyncMock(return_value=[]),
    ):
        result = await review_gmail_inbox(
            broker=broker, db=AsyncMock(), tenant_id=TENANT_ID, query="in:inbox", message_id=None
        )
    assert len(result.authorized) == 1


async def test_explicit_message_id_bypasses_sender_filter_via_user_requested() -> None:
    """Camino de mensaje puntual: user_requested=True, sin pasar por 'query'."""
    broker = _broker(
        message_ids=[],
        metadata_by_id={"m1": {"from": "cualquiera@x.com", "subject": "X", "label_ids": []}},
        full_by_id={"m1": {"from": "cualquiera@x.com", "subject": "X", "date": "hoy"}},
    )
    with patch(
        "app.application.services.gmail_inbox_review_service.get_approved_senders",
        new=AsyncMock(return_value=[]),
    ):
        result = await review_gmail_inbox(
            broker=broker, db=AsyncMock(), tenant_id=TENANT_ID, query=None, message_id="m1"
        )
    assert len(result.authorized) == 1
    # list_gmail_messages NUNCA se llama para el camino de id puntual.
    broker.list_gmail_messages.assert_not_called()


async def test_has_more_reflects_next_page_token() -> None:
    broker = _broker(
        message_ids=["m1"],
        next_page_token="tok-2",
        metadata_by_id={"m1": {"from": "spam@x.com", "subject": "X", "label_ids": []}},
    )
    with patch(
        "app.application.services.gmail_inbox_review_service.get_approved_senders",
        new=AsyncMock(return_value=[]),
    ):
        result = await review_gmail_inbox(
            broker=broker, db=AsyncMock(), tenant_id=TENANT_ID, query="in:inbox", message_id=None
        )
    assert result.has_more is True


async def test_metadata_fetch_failure_counts_as_failed_not_skipped() -> None:
    broker = _broker(message_ids=["m1"], metadata_by_id={})  # KeyError al buscar "m1"
    with patch(
        "app.application.services.gmail_inbox_review_service.get_approved_senders",
        new=AsyncMock(return_value=[]),
    ):
        result = await review_gmail_inbox(
            broker=broker, db=AsyncMock(), tenant_id=TENANT_ID, query="in:inbox", message_id=None
        )
    assert result.failed == 1
    assert not result.authorized
    assert not result.skipped


async def test_auth_error_propagates_instead_of_counting_as_failed() -> None:
    """Perder la conexión de Google no es 'un mensaje falló': el confirm

    endpoint necesita ver el McpToolAuthError para marcar REQUIRES_RECONNECT,
    no un conteo silencioso de fallos que oculta que hay que reconectar.
    """
    broker = _broker(message_ids=["m1"])
    broker.get_gmail_message = AsyncMock(
        side_effect=McpToolAuthError("expired", reason="refresh_failed")
    )
    with (
        patch(
            "app.application.services.gmail_inbox_review_service.get_approved_senders",
            new=AsyncMock(return_value=[]),
        ),
        pytest.raises(McpToolAuthError),
    ):
        await review_gmail_inbox(
            broker=broker, db=AsyncMock(), tenant_id=TENANT_ID, query="in:inbox", message_id=None
        )
