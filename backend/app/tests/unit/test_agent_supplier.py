"""Tests unitarios para AgentSupplier — sin llamadas reales al LLM ni a la DB."""

import json
import unittest.mock
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.application.agents.shared.schemas import AgentRequest, ActionType, RiskLevel


def _make_request(message: str = "revisar correo del proveedor") -> AgentRequest:
    return AgentRequest(
        user_id="user-123",
        business_id="tenant-456",
        message=message,
    )


def _mock_llm_response(classification: str, confidence: str = "HIGH", should_open_body: bool = False) -> MagicMock:
    content_block = MagicMock()
    content_block.text = json.dumps({
        "classification": classification,
        "confidence": confidence,
        "should_open_body": should_open_body,
    })
    response = MagicMock()
    response.content = [content_block]
    return response


# ── Pre-flight tests ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_preflight_blocks_unknown_sender():
    """Email de sender no registrado, sin label Véktor → False."""
    with unittest.mock.patch(
        "app.application.agents.supplier.preflight.get_approved_senders",
        new=AsyncMock(return_value=[]),
    ):
        from app.application.agents.supplier.preflight import gmail_preflight_check

        metadata = {
            "from": "desconocido@spam.com",
            "subject": "Oferta",
            "labels": ["INBOX"],
            "snippet": "...",
        }
        result = await gmail_preflight_check(
            metadata, business_id="tenant-456", db=MagicMock(), user_requested=False
        )
    assert result is False


@pytest.mark.asyncio
async def test_preflight_passes_vektor_label():
    """Email con label 'Véktor' pasa aunque el sender no esté registrado."""
    from app.application.agents.supplier.preflight import gmail_preflight_check

    metadata = {
        "from": "cualquiera@proveedor.com",
        "subject": "Hola",
        "labels": ["INBOX", "Véktor"],
        "snippet": "...",
    }
    # Sin db → salta condición 1, pasa por condición 2
    result = await gmail_preflight_check(
        metadata, business_id="tenant-456", db=None, user_requested=False
    )
    assert result is True


@pytest.mark.asyncio
async def test_preflight_passes_registered_supplier():
    """Email de proveedor registrado → True."""
    with unittest.mock.patch(
        "app.application.agents.supplier.preflight.get_approved_senders",
        new=AsyncMock(return_value=["test@proveedor.com"]),
    ):
        from app.application.agents.supplier.preflight import gmail_preflight_check

        metadata = {
            "from": "test@proveedor.com",
            "subject": "Lista de precios",
            "labels": ["INBOX"],
            "snippet": "Adjunto lista actualizada...",
        }
        result = await gmail_preflight_check(
            metadata, business_id="tenant-456", db=MagicMock(), user_requested=False
        )
    assert result is True


# ── Email classification tests ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_email_classification_returns_valid_category():
    """La clasificación vía LLM retorna una categoría dentro de EMAIL_CLASSIFICATIONS."""
    with unittest.mock.patch(
        "app.application.agents.supplier.agent.client"
    ) as mock_client:
        mock_client.messages.create.return_value = _mock_llm_response(
            "lista_precios", "HIGH", True
        )

        from app.application.agents.supplier.agent import AgentSupplier, EMAIL_CLASSIFICATIONS

        agent = AgentSupplier()
        metadata = {
            "from": "proveedor@ejemplo.com",
            "subject": "Lista de precios actualizada",
            "labels": ["INBOX"],
            "snippet": "Adjunto la lista de precios de esta semana.",
        }
        result = await agent.classify_email(metadata)

    assert result["classification"] in EMAIL_CLASSIFICATIONS
    assert result["confidence"] in ("HIGH", "MEDIUM", "LOW")
    assert isinstance(result["should_open_body"], bool)


# ── Draft guardrail tests ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_draft_creation_is_medium_risk():
    """_handle_email_request siempre retorna risk_level=MEDIUM y requires_approval=True."""
    from app.application.agents.supplier.agent import AgentSupplier

    agent = AgentSupplier()
    request = _make_request("hay un correo del proveedor")
    result = await agent.process(request)

    assert result.risk_level == RiskLevel.MEDIUM
    assert result.requires_approval is True
    assert result.status == "requires_approval"


@pytest.mark.asyncio
async def test_draft_never_sends_directly():
    """CREATE_SUPPLIER_DRAFT siempre aparece como pending_action — nunca envío directo."""
    from app.application.agents.supplier.agent import AgentSupplier

    agent = AgentSupplier()
    request = _make_request("respondé el email del proveedor")
    result = await agent.process(request)

    # El action_type debe ser CREATE_SUPPLIER_DRAFT y requires_approval=True
    assert result.result.get("action_type") == ActionType.CREATE_SUPPLIER_DRAFT
    assert result.requires_approval is True
    # Nunca debe haber un campo "sent" o indicación de envío directo
    assert "sent" not in result.result
    assert result.status != "success"  # siempre pendiente de aprobación


# ── Audit log tests ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_gmail_skipped_logged_in_audit():
    """Cuando preflight falla → GMAIL_SKIPPED se registra en audit_log."""
    with unittest.mock.patch(
        "app.application.agents.supplier.preflight.get_approved_senders",
        new=AsyncMock(return_value=[]),
    ):
        from app.application.agents.supplier.preflight import preflight_and_log

        metadata = {
            "from": "desconocido@spam.com",
            "subject": "Publicidad",
            "labels": ["INBOX"],
            "snippet": "...",
        }
        audit_logger = MagicMock()
        audit_logger.log = AsyncMock()

        result = await preflight_and_log(
            metadata,
            business_id="tenant-456",
            db=MagicMock(),
            audit_logger=audit_logger,
            user_requested=False,
        )

    assert result is False
    audit_logger.log.assert_called_once()
    call_kwargs = audit_logger.log.call_args.kwargs
    assert call_kwargs["action"] == "GMAIL_SKIPPED"
    assert call_kwargs["business_id"] == "tenant-456"
