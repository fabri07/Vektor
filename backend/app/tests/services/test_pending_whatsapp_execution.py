"""Tests de ejecución de PREPARE_WHATSAPP_MESSAGE en PendingActionService (v4 F6a).

Cubre:
- Ejecución exitosa: CommunicationLog escrito (channel=whatsapp, status=sent),
  ExternalOperationLog escrito (operation_type=whatsapp_clicklink),
  action.payload["result"]["url"] es un wa.me válido.
- Cross-tenant (OBLIGATORIO): recipient_id de otro tenant → ValueError, sin logs.

Nota: record_external_operation se parchea en su módulo original (external_operation_service),
no en pending_action_service, porque se importa localmente dentro del handler.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.application.agents.shared.schemas import ActionType
from app.application.services.pending_action_service import execute_pending_action

# Ruta de patch: módulo original (importación local usa sys.modules para resolución)
_PATCH_EXT_OP = "app.application.services.external_operation_service.record_external_operation"


def _make_action(payload: dict[str, Any]) -> Any:
    from app.persistence.models.pending_action import PendingAction  # noqa: PLC0415

    action = PendingAction()
    action.id = uuid.uuid4()
    action.tenant_id = uuid.uuid4()
    action.user_id = uuid.uuid4()
    action.action_type = ActionType.PREPARE_WHATSAPP_MESSAGE
    action.payload = payload
    action.risk_level = "MEDIUM"
    action.status = "APPROVED"
    action.external_system = None
    return action


def _make_customer(tenant_id: uuid.UUID, phone: str = "5491100001111") -> MagicMock:
    c = MagicMock()
    c.id = uuid.uuid4()
    c.tenant_id = tenant_id
    c.phone = phone
    c.is_sentinel = False
    return c


# ── Ejecución exitosa ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_whatsapp_escribe_logs_y_url():
    """Ejecución correcta: CommunicationLog + ExternalOperationLog + url en payload."""
    recipient_id = uuid.uuid4()
    action = _make_action(
        {
            "recipient_type": "customer",
            "recipient_id": str(recipient_id),
            "to": "5491100001111",
            "body": "Hola Juan, te recuerdo que tenés un saldo pendiente de $1.500.",
        }
    )

    customer = _make_customer(action.tenant_id, phone="5491100001111")
    customer.id = recipient_id

    # Resultado falso de db.execute().scalar_one_or_none() → customer
    mock_result = MagicMock()
    mock_result.scalar_one_or_none = MagicMock(return_value=customer)

    db = AsyncMock()
    db.execute = AsyncMock(return_value=mock_result)

    added_objects: list[Any] = []

    def _add(obj: Any) -> None:
        added_objects.append(obj)

    db.add = MagicMock(side_effect=_add)

    with patch(_PATCH_EXT_OP, new_callable=AsyncMock) as mock_ext_op:
        mock_ext_op.return_value = MagicMock()
        await execute_pending_action(action, db)

    # CommunicationLog debe haberse añadido
    from app.persistence.models.communication_log import CommunicationLog  # noqa: PLC0415

    comm_logs = [o for o in added_objects if isinstance(o, CommunicationLog)]
    assert len(comm_logs) >= 1, "Debe crearse al menos un CommunicationLog"
    comm = comm_logs[0]
    assert comm.channel == "whatsapp"
    assert comm.status == "sent"
    assert comm.recipient_id == recipient_id
    assert comm.recipient_type == "customer"
    assert "Juan" in comm.body

    # ExternalOperationLog debe haberse llamado
    mock_ext_op.assert_called_once()
    call_kwargs = mock_ext_op.call_args.kwargs
    assert call_kwargs["operation_type"] == "whatsapp_clicklink"
    assert call_kwargs["provider"] == "whatsapp"
    assert call_kwargs["status"] == "sent"

    # payload["result"]["url"] debe ser un link wa.me válido
    result = action.payload.get("result")
    assert result is not None, "action.payload debe tener 'result' tras la ejecución"
    url = result["url"]
    assert url.startswith("https://wa.me/"), f"URL no es wa.me: {url}"
    assert "?text=" in url
    assert result["channel"] == "whatsapp"


@pytest.mark.asyncio
async def test_url_contiene_telefono_y_cuerpo():
    """El link wa.me debe contener el teléfono normalizado y el cuerpo encodeado."""
    recipient_id = uuid.uuid4()
    body = "Hola Ana, tenés un saldo de $500."
    action = _make_action(
        {
            "recipient_type": "customer",
            "recipient_id": str(recipient_id),
            "to": "1100001234",  # sin prefijo 54
            "body": body,
        }
    )

    customer = _make_customer(action.tenant_id, phone="1100001234")
    customer.id = recipient_id

    mock_result = MagicMock()
    mock_result.scalar_one_or_none = MagicMock(return_value=customer)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=mock_result)
    db.add = MagicMock()

    with patch(_PATCH_EXT_OP, new_callable=AsyncMock):
        await execute_pending_action(action, db)

    url = action.payload["result"]["url"]
    # Teléfono sin 54 → se antepone "54"
    assert "wa.me/541100001234" in url
    # Cuerpo encodeado
    assert "?text=" in url


# ── Cross-tenant (OBLIGATORIO) ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cross_tenant_falla_sin_logs():
    """recipient_id de otro tenant → ValueError, sin CommunicationLog, sin ExtOpLog."""
    recipient_id = uuid.uuid4()
    action = _make_action(
        {
            "recipient_type": "customer",
            "recipient_id": str(recipient_id),
            "to": "5491100001111",
            "body": "Hola, te recuerdo que me debés.",
        }
    )

    # scalar_one_or_none devuelve None → el customer NO existe en este tenant
    mock_result = MagicMock()
    mock_result.scalar_one_or_none = MagicMock(return_value=None)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=mock_result)

    added_objects: list[Any] = []
    db.add = MagicMock(side_effect=added_objects.append)

    with patch(_PATCH_EXT_OP, new_callable=AsyncMock) as mock_ext_op:
        # La ejecución debe levantarse con ValueError
        with pytest.raises(ValueError, match="recipient_not_in_tenant"):
            await execute_pending_action(action, db)

    # No debe haberse escrito ningún log
    from app.persistence.models.communication_log import CommunicationLog  # noqa: PLC0415

    comm_logs = [o for o in added_objects if isinstance(o, CommunicationLog)]
    assert len(comm_logs) == 0, "No debe crearse CommunicationLog para un cross-tenant"
    mock_ext_op.assert_not_called()

    # Tampoco debe haber resultado en el payload
    assert "result" not in (action.payload or {})
