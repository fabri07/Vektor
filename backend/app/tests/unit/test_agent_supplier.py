"""Tests unitarios para AgentSupplier — sin llamadas reales al LLM ni a la DB."""

from __future__ import annotations

import json
import unittest.mock
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.application.agents.shared.schemas import AgentRequest, ActionType, RiskLevel
from app.integrations.google_workspace.gmail_client import GmailMessage, GmailMessageSummary


def _make_request(message: str = "revisar correo del proveedor") -> AgentRequest:
    return AgentRequest(
        user_id="user-123",
        business_id="tenant-456",
        message=message,
    )


def _make_llm_json(classification: str, confidence: str = "HIGH", should_open_body: bool = False) -> str:
    return json.dumps({
        "classification": classification,
        "confidence": confidence,
        "should_open_body": should_open_body,
    })


def _make_msg_summary(
    from_: str = "proveedor@ejemplo.com",
    subject: str = "Lista de precios",
    labels: list[str] | None = None,
) -> GmailMessageSummary:
    return GmailMessageSummary(
        message_id="msg1",
        thread_id="thread1",
        subject=subject,
        from_=from_,
        snippet="Adjunto lista actualizada.",
        date="Mon, 6 Apr 2026 10:00:00 -0300",
        labels=labels or ["INBOX", "Véktor"],
    )


def _make_full_msg(summary: GmailMessageSummary | None = None) -> GmailMessage:
    s = summary or _make_msg_summary()
    return GmailMessage(
        message_id=s.message_id,
        thread_id=s.thread_id,
        subject=s.subject,
        from_=s.from_,
        snippet=s.snippet,
        date=s.date,
        labels=s.labels,
        body_text="Texto del correo con detalle de la lista de precios.",
    )


def _mock_gateway(
    *,
    connected: bool = True,
    messages: list[GmailMessageSummary] | None = None,
    full_msg: GmailMessage | None = None,
) -> MagicMock:
    """Crea un gateway mock listo para el flujo de email."""
    gateway = MagicMock()
    gateway.is_connected = AsyncMock(return_value=connected)

    gmail_mock = AsyncMock()
    gmail_mock.list_messages = AsyncMock(return_value=messages or [_make_msg_summary()])
    gmail_mock.get_message = AsyncMock(return_value=full_msg or _make_full_msg())
    gateway.gmail = AsyncMock(return_value=gmail_mock)

    # run_gmail: ejecuta la coroutine pasada
    async def _passthrough(coro):
        return await coro

    gateway.run_gmail = _passthrough
    return gateway


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
    from app.application.agents.supplier.agent import AgentSupplier, EMAIL_CLASSIFICATIONS

    with patch("anthropic.AsyncAnthropic") as mock_cls:
        # Configurar instancia async mock
        mock_instance = AsyncMock()
        mock_cls.return_value = mock_instance

        content_block = MagicMock()
        content_block.text = _make_llm_json("lista_precios", "HIGH", True)
        llm_response = MagicMock()
        llm_response.content = [content_block]
        mock_instance.messages.create = AsyncMock(return_value=llm_response)

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
    """Cuando hay emails que necesitan respuesta, retorna risk_level=MEDIUM y requires_approval=True."""
    from app.application.agents.supplier.agent import AgentSupplier

    gateway = _mock_gateway()

    with (
        patch("anthropic.AsyncAnthropic") as mock_cls,
        patch(
            "app.application.agents.supplier.agent.gmail_preflight_check",
            new=AsyncMock(return_value=True),
        ),
    ):
        mock_instance = AsyncMock()
        mock_cls.return_value = mock_instance

        # classify_email → should_open_body=True para triggear el draft
        content_block = MagicMock()
        content_block.text = _make_llm_json("lista_precios", "HIGH", should_open_body=True)
        draft_block = MagicMock()
        draft_block.text = "Estimado proveedor, gracias por su lista de precios."
        mock_instance.messages.create = AsyncMock(
            side_effect=[
                MagicMock(content=[content_block]),  # classify_email
                MagicMock(content=[draft_block]),    # create_draft
            ]
        )

        agent = AgentSupplier(gateway=gateway)
        result = await agent.process(_make_request("revisar correo del proveedor"))

    assert result.risk_level == RiskLevel.MEDIUM
    assert result.requires_approval is True
    assert result.status == "requires_approval"


@pytest.mark.asyncio
async def test_draft_never_sends_directly():
    """CREATE_SUPPLIER_DRAFT siempre aparece como pending_action — nunca envío directo."""
    from app.application.agents.supplier.agent import AgentSupplier

    gateway = _mock_gateway()

    with (
        patch("anthropic.AsyncAnthropic") as mock_cls,
        patch(
            "app.application.agents.supplier.agent.gmail_preflight_check",
            new=AsyncMock(return_value=True),
        ),
    ):
        mock_instance = AsyncMock()
        mock_cls.return_value = mock_instance

        content_block = MagicMock()
        content_block.text = _make_llm_json("factura", "HIGH", should_open_body=True)
        draft_block = MagicMock()
        draft_block.text = "Confirmamos recepción de la factura."
        mock_instance.messages.create = AsyncMock(
            side_effect=[
                MagicMock(content=[content_block]),
                MagicMock(content=[draft_block]),
            ]
        )

        agent = AgentSupplier(gateway=gateway)
        result = await agent.process(_make_request("respondé el email del proveedor"))

    assert result.result.get("action_type") == ActionType.CREATE_SUPPLIER_DRAFT
    assert result.requires_approval is True
    assert "sent" not in result.result
    assert result.status != "success"


@pytest.mark.asyncio
async def test_process_without_gateway_returns_requires_clarification():
    """Sin gateway inyectado → requires_clarification con reconnect_required."""
    from app.application.agents.supplier.agent import AgentSupplier

    agent = AgentSupplier(gateway=None)
    result = await agent.process(_make_request("revisar correo del proveedor"))

    assert result.status == "requires_clarification"
    assert result.result.get("reconnect_required") is True
    assert result.result.get("reason") == "not_connected"


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
