"""Tests de handlers Stage 4 en PendingActionService.

Cubre: UPLOAD_TO_DRIVE, CREATE_GOOGLE_DOC, APPEND_TO_SHEET.
Los tres ActionTypes usan GoogleToolBroker; mockear _make_google_broker es suficiente.
"""

from __future__ import annotations

import base64
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.application.agents.google.agent import AgentGoogle
from app.application.agents.shared.schemas import ActionType, AgentRequest, AgentTask
from app.application.services.pending_action_service import execute_pending_action

_PATCH = "app.application.services.pending_action_service._make_google_broker"


def _make_action(action_type: str, payload: dict[str, Any]):
    from app.persistence.models.pending_action import PendingAction  # noqa: PLC0415

    action = PendingAction()
    action.id = uuid.uuid4()
    action.tenant_id = uuid.uuid4()
    action.user_id = uuid.uuid4()
    action.action_type = action_type
    action.payload = payload
    action.risk_level = "MEDIUM"
    action.status = "APPROVED"
    action.external_system = None
    return action


def _make_broker(**overrides):
    broker = AsyncMock()
    broker.upload_drive_file = AsyncMock(return_value={"id": "file123", "name": "test.txt"})
    broker.create_doc = AsyncMock(return_value={"document_id": "doc456"})
    broker.append_doc_content = AsyncMock(return_value={})
    broker.append_sheet_rows = AsyncMock(return_value={})
    for k, v in overrides.items():
        setattr(broker, k, v)
    return broker


# ── UPLOAD_TO_DRIVE ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_to_drive_calls_broker():
    action = _make_action(
        ActionType.UPLOAD_TO_DRIVE,
        {"filename": "informe.txt", "content_base64": "aGVsbG8=", "mime_type": "text/plain"},
    )
    broker = _make_broker()
    db = MagicMock()

    with patch(_PATCH, return_value=broker):
        await execute_pending_action(action, db)

    broker.upload_drive_file.assert_called_once_with(
        name="informe.txt",
        content_base64="aGVsbG8=",
        mime_type="text/plain",
        folder_id=None,
    )
    assert action.external_system == "GOOGLE_DRIVE"


@pytest.mark.asyncio
async def test_upload_to_drive_default_name_when_no_filename():
    action = _make_action(
        ActionType.UPLOAD_TO_DRIVE,
        {"content_base64": "dGVzdA==", "mime_type": "text/plain"},
    )
    broker = _make_broker()
    db = MagicMock()

    with patch(_PATCH, return_value=broker):
        await execute_pending_action(action, db)

    call_kwargs = broker.upload_drive_file.call_args.kwargs
    assert call_kwargs["name"] == "archivo.bin"  # default cuando no hay filename ni name


@pytest.mark.asyncio
async def test_upload_to_drive_with_folder_id():
    action = _make_action(
        ActionType.UPLOAD_TO_DRIVE,
        {
            "filename": "doc.pdf",
            "content_base64": "cGRm",
            "mime_type": "application/pdf",
            "folder_id": "folder789",
        },
    )
    broker = _make_broker()
    db = MagicMock()

    with patch(_PATCH, return_value=broker):
        await execute_pending_action(action, db)

    broker.upload_drive_file.assert_called_once_with(
        name="doc.pdf",
        content_base64="cGRm",
        mime_type="application/pdf",
        folder_id="folder789",
    )


# ── CREATE_GOOGLE_DOC ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_google_doc_with_content():
    action = _make_action(
        ActionType.CREATE_GOOGLE_DOC,
        {"title": "Reporte Enero", "content": "Score: 82/100. Caja saludable."},
    )
    broker = _make_broker()
    db = MagicMock()

    with patch(_PATCH, return_value=broker):
        await execute_pending_action(action, db)

    broker.create_doc.assert_called_once_with(title="Reporte Enero")
    broker.append_doc_content.assert_called_once_with(
        document_id="doc456",
        content="Score: 82/100. Caja saludable.",
    )
    assert action.external_system == "GOOGLE_DOCS"


@pytest.mark.asyncio
async def test_create_google_doc_without_content_skips_append():
    action = _make_action(
        ActionType.CREATE_GOOGLE_DOC,
        {"title": "Documento vacío"},
    )
    broker = _make_broker()
    db = MagicMock()

    with patch(_PATCH, return_value=broker):
        await execute_pending_action(action, db)

    broker.create_doc.assert_called_once_with(title="Documento vacío")
    broker.append_doc_content.assert_not_called()


@pytest.mark.asyncio
async def test_create_google_doc_default_title():
    action = _make_action(ActionType.CREATE_GOOGLE_DOC, {})
    broker = _make_broker()
    db = MagicMock()

    with patch(_PATCH, return_value=broker):
        await execute_pending_action(action, db)

    broker.create_doc.assert_called_once_with(title="Documento Véktor")


# ── APPEND_TO_SHEET ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_append_to_sheet_calls_broker():
    rows = [[100, "gaseosa", "2026-05-26"], [200, "agua", "2026-05-26"]]
    action = _make_action(
        ActionType.APPEND_TO_SHEET,
        {"spreadsheet_id": "sheet123", "range_name": "Ventas!A1", "rows": rows},
    )
    broker = _make_broker()
    db = MagicMock()

    with patch(_PATCH, return_value=broker):
        await execute_pending_action(action, db)

    broker.append_sheet_rows.assert_called_once_with(
        spreadsheet_id="sheet123",
        range_name="Ventas!A1",
        rows=rows,
    )
    assert action.external_system == "GOOGLE_SHEETS"


@pytest.mark.asyncio
async def test_append_to_sheet_default_range():
    action = _make_action(
        ActionType.APPEND_TO_SHEET,
        {"spreadsheet_id": "sheet456", "rows": [[1, 2]]},
    )
    broker = _make_broker()
    db = MagicMock()

    with patch(_PATCH, return_value=broker):
        await execute_pending_action(action, db)

    broker.append_sheet_rows.assert_called_once_with(
        spreadsheet_id="sheet456",
        range_name="Sheet1",  # default
        rows=[[1, 2]],
    )


# ── AgentGoogle — lectura de upstream_outputs ─────────────────────────────────


def test_agent_google_upload_reads_upstream_outputs():
    """AgentGoogle popula content_base64 desde el health report del upstream DAG."""
    health_task_id = str(uuid.uuid4())
    request = AgentRequest(
        user_id=str(uuid.uuid4()),
        business_id=str(uuid.uuid4()),
        message="subí el informe a Drive",
        context={
            "upstream_outputs": {
                health_task_id: {
                    "summary": "Score excelente. Caja en buen nivel.",
                    "health_score": 87,
                }
            }
        },
    )
    task = AgentTask(
        task_id=str(uuid.uuid4()),
        agent="agent_google",
        action_type=ActionType.UPLOAD_TO_DRIVE,
        entities={},
    )
    agent = AgentGoogle()
    response = agent._pending_for_broker_action(request, ActionType.UPLOAD_TO_DRIVE, task)

    assert response.requires_approval is True
    payload = response.result["payload"]
    assert payload["content_base64"] != ""
    decoded = base64.b64decode(payload["content_base64"]).decode()
    assert "Score excelente" in decoded
    assert "87/100" in decoded
    assert payload["filename"] == "informe_salud_vektor.txt"
    assert payload["mime_type"] == "text/plain"


def test_agent_google_upload_without_upstream_keeps_empty_payload():
    """Sin upstream_outputs, el payload de UPLOAD_TO_DRIVE no inventa datos."""
    request = AgentRequest(
        user_id=str(uuid.uuid4()),
        business_id=str(uuid.uuid4()),
        message="subí a Drive",
    )
    task = AgentTask(
        task_id=str(uuid.uuid4()),
        agent="agent_google",
        action_type=ActionType.UPLOAD_TO_DRIVE,
        entities={},
    )
    agent = AgentGoogle()
    response = agent._pending_for_broker_action(request, ActionType.UPLOAD_TO_DRIVE, task)

    payload = response.result["payload"]
    # No content_base64 inyectado — se deja vacío para que el usuario lo complete
    assert "content_base64" not in payload
