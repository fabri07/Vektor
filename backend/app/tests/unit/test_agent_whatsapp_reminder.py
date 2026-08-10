"""Tests para AgentClient._handle_whatsapp_reminder (v4 F6a).

Cubre:
- Con un deudor (balance>0, phone) → requires_approval con structured_data correcto
- Sin deudores → status success, mensaje no-invention, sin PendingAction
- Deudor sin phone → requires_clarification
- Sin DB → respuesta amigable, no revienta
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from app.application.agents.shared.schemas import (
    ActionType,
    AgentRequest,
    AgentTask,
)

_TENANT = "00000000-0000-0000-0000-000000000001"
_USER = "00000000-0000-0000-0000-000000000002"
_CUSTOMER_ID = "00000000-0000-0000-0000-000000000010"
_PHONE = "5491100001111"


def _req(tenant_id: str = _TENANT) -> AgentRequest:
    return AgentRequest(user_id=_USER, business_id=tenant_id, message="mandá un wpp de cobranza")


def _task(entities: dict[str, Any] | None = None) -> AgentTask:
    return AgentTask(
        agent="agent_client",
        action_type=ActionType.PREPARE_WHATSAPP_MESSAGE,
        entities=entities or {},
    )


def _mock_customer(*, name: str = "Juan", phone: str | None = _PHONE) -> MagicMock:
    c = MagicMock()
    c.id = __import__("uuid").UUID(_CUSTOMER_ID)
    c.name = name
    c.phone = phone
    c.is_sentinel = False
    return c


# ── Sin DB → amigable, no revienta ───────────────────────────────────────────


async def test_sin_db_devuelve_mensaje_amigable():
    from app.application.agents.client.agent import AgentClient

    agent = AgentClient(db=None)
    resp = await agent.process(_req(), task=_task())

    assert resp.status == "success"
    assert resp.requires_approval is False
    assert "recordatorio" in (resp.message or "").lower()


# ── Con deudor (balance>0, phone) → requires_approval ────────────────────────


async def test_con_deudor_emite_requires_approval():
    """El mayor deudor con phone → requires_approval con structured_data correcto."""
    from app.application.agents.client.agent import AgentClient

    mock_db = MagicMock()
    agent = AgentClient(db=mock_db)

    balances = [
        {
            "customer_id": __import__("uuid").UUID(_CUSTOMER_ID),
            "customer_name": "Juan",
            "balance": 1500.0,
        }
    ]
    customer = _mock_customer(name="Juan", phone=_PHONE)

    with (
        patch(
            "app.persistence.repositories.customer_repository.CustomerRepository"
        ) as MockCR,
        patch(
            "app.persistence.repositories.transaction_repository.SaleRepository"
        ) as MockSR,
    ):
        cr_instance = AsyncMock()
        cr_instance.get_by_id = AsyncMock(return_value=customer)
        MockCR.return_value = cr_instance

        sr_instance = AsyncMock()
        sr_instance.get_balances_by_customer = AsyncMock(return_value=balances)
        MockSR.return_value = sr_instance

        resp = await agent.process(_req(), task=_task())

    assert resp.status == "requires_approval"
    assert resp.requires_approval is True
    assert resp.risk_level == "MEDIUM"

    # structured_data debe tener recipient_id, to, body
    sd = resp.result["structured_data"]
    assert sd["recipient_type"] == "customer"
    assert sd["recipient_id"] == _CUSTOMER_ID
    assert sd["to"] == _PHONE
    # El cuerpo debe contener el nombre y el monto con formato AR
    assert "Juan" in sd["body"]
    assert "1.500" in sd["body"]  # formato AR: punto como separador de miles
    # Regresión F6a Finding 1: la coma del saludo no debe ser reemplazada por punto
    assert "Hola Juan, te recuerdo" in sd["body"], (
        "La coma del saludo fue reemplazada por punto — revisar .replace(',', '.')"
    )


async def test_body_determinista_contiene_nombre_y_monto():
    """El cuerpo es determinístico: incluye nombre y saldo formateado AR."""
    from app.application.agents.client.agent import AgentClient

    mock_db = MagicMock()
    agent = AgentClient(db=mock_db)

    balances = [
        {
            "customer_id": __import__("uuid").UUID(_CUSTOMER_ID),
            "customer_name": "María",
            "balance": 3000.0,
        }
    ]
    customer = _mock_customer(name="María", phone="5491199998888")

    with (
        patch(
            "app.persistence.repositories.customer_repository.CustomerRepository"
        ) as MockCR,
        patch(
            "app.persistence.repositories.transaction_repository.SaleRepository"
        ) as MockSR,
    ):
        cr_instance = AsyncMock()
        cr_instance.get_by_id = AsyncMock(return_value=customer)
        MockCR.return_value = cr_instance

        sr_instance = AsyncMock()
        sr_instance.get_balances_by_customer = AsyncMock(return_value=balances)
        MockSR.return_value = sr_instance

        resp = await agent.process(_req(), task=_task())

    sd = resp.result["structured_data"]
    assert "María" in sd["body"]
    assert "3.000" in sd["body"]


# ── Cliente nombrado con balance = 0 → no-invention (Finding 2) ──────────────


async def test_cliente_nombrado_con_balance_cero_no_crea_pending():
    """Cliente específico resuelto por id pero con balance=0 → success sin PendingAction.

    Regresión: el path de customer_id explícito no tenía guard de balance>0,
    lo que permitía enviar 'saldo pendiente de $0'.
    """
    from app.application.agents.client.agent import AgentClient

    mock_db = MagicMock()
    agent = AgentClient(db=mock_db)

    customer = _mock_customer(name="Juan", phone=_PHONE)

    with (
        patch(
            "app.persistence.repositories.customer_repository.CustomerRepository"
        ) as MockCR,
        patch(
            "app.persistence.repositories.transaction_repository.SaleRepository"
        ) as MockSR,
    ):
        cr_instance = AsyncMock()
        cr_instance.get_by_id = AsyncMock(return_value=customer)
        MockCR.return_value = cr_instance

        sr_instance = AsyncMock()
        # Sin entradas en balances → balance queda en 0
        sr_instance.get_balances_by_customer = AsyncMock(return_value=[])
        MockSR.return_value = sr_instance

        resp = await agent.process(
            _req(), task=_task(entities={"customer_id": _CUSTOMER_ID})
        )

    assert resp.status == "success", f"Esperado success, obtenido: {resp.status}"
    assert resp.requires_approval is False
    assert "pendiente" in (resp.message or "").lower()
    # No debe haber structured_data con body de recordatorio
    if resp.result:
        sd = (resp.result or {}).get("structured_data", {})
        assert not sd.get("body"), "No debe generarse cuerpo de mensaje con balance=0"


# ── Sin deudores → no-invention ───────────────────────────────────────────────


async def test_sin_deudores_no_invention():
    """Sin deudores → status success, mensaje claro, sin requires_approval."""
    from app.application.agents.client.agent import AgentClient

    mock_db = MagicMock()
    agent = AgentClient(db=mock_db)

    with (
        patch(
            "app.persistence.repositories.customer_repository.CustomerRepository"
        ) as MockCR,
        patch(
            "app.persistence.repositories.transaction_repository.SaleRepository"
        ) as MockSR,
    ):
        cr_instance = AsyncMock()
        MockCR.return_value = cr_instance

        sr_instance = AsyncMock()
        # Balance 0 o negativo → no hay deudores
        sr_instance.get_balances_by_customer = AsyncMock(
            return_value=[
                {"customer_id": __import__("uuid").UUID(_CUSTOMER_ID), "balance": 0.0}
            ]
        )
        MockSR.return_value = sr_instance

        resp = await agent.process(_req(), task=_task())

    assert resp.status == "success"
    assert resp.requires_approval is False
    assert "pendiente" in (resp.message or "").lower()


async def test_balances_vacios_no_invention():
    """Lista de balances vacía → no-invention, sin requires_approval."""
    from app.application.agents.client.agent import AgentClient

    mock_db = MagicMock()
    agent = AgentClient(db=mock_db)

    with (
        patch(
            "app.persistence.repositories.customer_repository.CustomerRepository"
        ) as MockCR,
        patch(
            "app.persistence.repositories.transaction_repository.SaleRepository"
        ) as MockSR,
    ):
        cr_instance = AsyncMock()
        MockCR.return_value = cr_instance

        sr_instance = AsyncMock()
        sr_instance.get_balances_by_customer = AsyncMock(return_value=[])
        MockSR.return_value = sr_instance

        resp = await agent.process(_req(), task=_task())

    assert resp.status == "success"
    assert resp.requires_approval is False


# ── Deudor sin phone → requires_clarification ────────────────────────────────


async def test_deudor_sin_phone_pide_completar_ficha():
    """Cliente con saldo pero sin phone → requires_clarification, no crea PA."""
    from app.application.agents.client.agent import AgentClient

    mock_db = MagicMock()
    agent = AgentClient(db=mock_db)

    balances = [
        {
            "customer_id": __import__("uuid").UUID(_CUSTOMER_ID),
            "customer_name": "Pedro",
            "balance": 800.0,
        }
    ]
    customer_sin_phone = _mock_customer(name="Pedro", phone=None)

    with (
        patch(
            "app.persistence.repositories.customer_repository.CustomerRepository"
        ) as MockCR,
        patch(
            "app.persistence.repositories.transaction_repository.SaleRepository"
        ) as MockSR,
    ):
        cr_instance = AsyncMock()
        cr_instance.get_by_id = AsyncMock(return_value=customer_sin_phone)
        MockCR.return_value = cr_instance

        sr_instance = AsyncMock()
        sr_instance.get_balances_by_customer = AsyncMock(return_value=balances)
        MockSR.return_value = sr_instance

        resp = await agent.process(_req(), task=_task())

    assert resp.status == "requires_clarification"
    assert resp.requires_approval is False
    assert "teléfono" in (resp.message or "").lower()
