from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from app.security import RequestContext
from app.tools import gmail

CTX = RequestContext(tenant_id=uuid.uuid4(), user_id=uuid.uuid4())


def _patch_token():
    return patch(
        "app.tools.gmail.get_valid_access_token",
        new=AsyncMock(return_value=("fake-token", None)),
    )


def _mock_get_response(json_body: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = json_body
    return resp


def _patch_httpx_get(json_body: dict):
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    captured: dict = {}

    async def _get(url: str, **kwargs):
        captured["url"] = url
        captured["params"] = kwargs.get("params")
        return _mock_get_response(json_body)

    mock_client.get = _get
    return patch("httpx.AsyncClient", return_value=mock_client), captured


async def test_list_messages_returns_message_ids_not_messages() -> None:
    """El server SIEMPRE devolvió message_ids -- nunca 'messages'.

    Este test fija el contrato real para que el lado backend (que asumía
    'messages') no vuelva a divergir en silencio.
    """
    body = {
        "messages": [{"id": "m1"}, {"id": "m2"}],
        "resultSizeEstimate": 2,
        "nextPageToken": "tok",
    }
    patcher, _ = _patch_httpx_get(body)
    with _patch_token(), patcher:
        result = await gmail.list_messages(session=object(), ctx=CTX, query="in:inbox")

    assert result == {
        "message_ids": ["m1", "m2"],
        "result_size_estimate": 2,
        "next_page_token": "tok",
    }
    assert "messages" not in result


async def test_get_message_metadata_excludes_body_and_snippet() -> None:
    body = {
        "id": "m1",
        "threadId": "t1",
        "labelIds": ["INBOX", "Label_9"],
        "snippet": "no deberia aparecer",
        "payload": {
            "headers": [
                {"name": "From", "value": "Proveedor <p@x.com>"},
                {"name": "Subject", "value": "Factura"},
            ]
        },
    }
    patcher, captured = _patch_httpx_get(body)
    with _patch_token(), patcher:
        result = await gmail.get_message(
            session=object(), ctx=CTX, message_id="m1", format_="metadata"
        )

    assert result == {
        "id": "m1",
        "thread_id": "t1",
        "subject": "Factura",
        "from": "Proveedor <p@x.com>",
        "label_ids": ["INBOX", "Label_9"],
    }
    assert "snippet" not in result
    assert "body_preview" not in result
    assert captured["params"]["format"] == "metadata"
    assert captured["params"]["metadataHeaders"] == ["From", "Subject"]


async def test_get_message_full_keeps_existing_contract() -> None:
    body = {
        "id": "m1",
        "threadId": "t1",
        "labelIds": ["INBOX"],
        "snippet": "hola",
        "payload": {
            "mimeType": "text/plain",
            "headers": [{"name": "From", "value": "p@x.com"}],
            "body": {"data": ""},
        },
    }
    patcher, captured = _patch_httpx_get(body)
    with _patch_token(), patcher:
        result = await gmail.get_message(session=object(), ctx=CTX, message_id="m1")

    assert result["snippet"] == "hola"
    assert "body_preview" in result
    assert captured["params"]["format"] == "full"
    assert "metadataHeaders" not in captured["params"]


async def test_list_labels_resolves_id_and_name() -> None:
    body = {
        "labels": [
            {"id": "INBOX", "name": "INBOX", "type": "system"},
            {"id": "Label_170555", "name": "Véktor", "type": "user"},
        ]
    }
    patcher, _ = _patch_httpx_get(body)
    with _patch_token(), patcher:
        result = await gmail.list_labels(session=object(), ctx=CTX)

    assert result == {
        "labels": [
            {"id": "INBOX", "name": "INBOX", "type": "system"},
            {"id": "Label_170555", "name": "Véktor", "type": "user"},
        ]
    }
