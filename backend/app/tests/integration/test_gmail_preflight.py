"""Tests WF-05: Gmail preflight check.

Verifica que gmail_preflight_check bloquea senders no registrados y
permite el paso cuando se cumple al menos una de las 3 condiciones.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_wf05_unknown_sender_no_db_blocked() -> None:
    """Sender desconocido sin db ni label → GMAIL_SKIPPED (False)."""
    from app.application.agents.supplier.preflight import gmail_preflight_check

    metadata = {
        "from": "spam@unknown.com",
        "subject": "Oferta imperdible",
        "labels": ["INBOX"],
        "snippet": "Tenemos una oferta...",
    }
    result = await gmail_preflight_check(metadata, "any-tenant-id")
    assert result is False


@pytest.mark.asyncio
async def test_wf05_registered_sender_passes() -> None:
    """Sender registrado en DB → True (pasa el preflight)."""
    from app.application.agents.supplier.preflight import gmail_preflight_check

    metadata = {
        "from": "proveedor@conocido.com",
        "subject": "Factura",
        "labels": ["INBOX"],
        "snippet": "Adjunto factura...",
    }
    fake_db = object()  # db truthy para activar condición 1
    with patch(
        "app.application.agents.supplier.preflight.get_approved_senders",
        new=AsyncMock(return_value=["proveedor@conocido.com"]),
    ):
        result = await gmail_preflight_check(metadata, "any-tenant-id", db=fake_db)
    assert result is True


@pytest.mark.asyncio
async def test_vektor_label_passes_without_db() -> None:
    """Email con label 'Véktor' pasa sin importar el sender ni la DB."""
    from app.application.agents.supplier.preflight import gmail_preflight_check

    metadata = {
        "from": "desconocido@example.com",
        "subject": "Pedido",
        "labels": ["INBOX", "Véktor"],
        "snippet": "Adjunto pedido",
    }
    result = await gmail_preflight_check(metadata, "any-tenant-id")
    assert result is True


@pytest.mark.asyncio
async def test_vektor_label_without_tilde_passes() -> None:
    """Label 'Vektor' (sin tilde) también pasa."""
    from app.application.agents.supplier.preflight import gmail_preflight_check

    metadata = {
        "from": "cualquiera@example.com",
        "subject": "Test",
        "labels": ["INBOX", "Vektor"],
        "snippet": "",
    }
    result = await gmail_preflight_check(metadata, "any-tenant-id")
    assert result is True


@pytest.mark.asyncio
async def test_user_requested_passes() -> None:
    """user_requested=True pasa siempre, independientemente del sender."""
    from app.application.agents.supplier.preflight import gmail_preflight_check

    metadata = {
        "from": "desconocido@spam.com",
        "subject": "Cualquier cosa",
        "labels": ["INBOX"],
        "snippet": "",
    }
    result = await gmail_preflight_check(
        metadata, "any-tenant-id", user_requested=True
    )
    assert result is True
