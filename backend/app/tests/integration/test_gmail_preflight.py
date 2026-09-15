"""Tests WF-05: Gmail preflight check.

Verifica que gmail_preflight_check bloquea senders no registrados y
permite el paso cuando se cumple al menos una de las 3 condiciones.
"""

from __future__ import annotations

from app.application.agents.supplier.preflight import (
    gmail_preflight_check,
    normalize_gmail_sender,
)


def test_normalize_gmail_sender_extracts_address_from_display_name() -> None:
    """'Empresa S.A. <compras@proveedor.com>' -> 'compras@proveedor.com'."""
    assert normalize_gmail_sender("Empresa S.A. <compras@proveedor.com>") == "compras@proveedor.com"


def test_normalize_gmail_sender_bare_address() -> None:
    assert normalize_gmail_sender("compras@proveedor.com") == "compras@proveedor.com"


def test_normalize_gmail_sender_empty() -> None:
    assert normalize_gmail_sender("") == ""


async def test_wf05_unknown_sender_no_match_blocked() -> None:
    """Sender desconocido, sin label Véktor ni user_requested → GMAIL_SKIPPED (False)."""
    metadata = {
        "from": "spam@unknown.com",
        "subject": "Oferta imperdible",
        "label_ids": ["INBOX"],
    }
    result = await gmail_preflight_check(
        metadata,
        approved_senders=frozenset(),
        vektor_label_ids=frozenset(),
    )
    assert result is False


async def test_wf05_registered_sender_passes() -> None:
    """Sender registrado (comparado ya normalizado) → True."""
    metadata = {
        "from": "Proveedor Conocido <proveedor@conocido.com>",
        "subject": "Factura",
        "label_ids": ["INBOX"],
    }
    result = await gmail_preflight_check(
        metadata,
        approved_senders=frozenset({"proveedor@conocido.com"}),
        vektor_label_ids=frozenset(),
    )
    assert result is True


async def test_vektor_label_passes_by_resolved_id_not_display_name() -> None:
    """La label se matchea por ID OPACO resuelto, nunca por el nombre 'Véktor'."""
    metadata = {
        "from": "desconocido@example.com",
        "subject": "Pedido",
        "label_ids": ["INBOX", "Label_170555"],
    }
    # El nombre "Véktor" nunca aparece en label_ids -- solo su id resuelto.
    result = await gmail_preflight_check(
        metadata,
        approved_senders=frozenset(),
        vektor_label_ids=frozenset({"Label_170555"}),
    )
    assert result is True


async def test_vektor_label_name_alone_never_matches() -> None:
    """Comparar contra el nombre 'Véktor' (no contra un id resuelto) NO debe pasar."""
    metadata = {
        "from": "desconocido@example.com",
        "subject": "Pedido",
        "label_ids": ["INBOX", "Label_170555"],
    }
    result = await gmail_preflight_check(
        metadata,
        approved_senders=frozenset(),
        vektor_label_ids=frozenset({"Véktor"}),  # id resuelto incorrecto a propósito
    )
    assert result is False


async def test_user_requested_passes_for_explicit_message() -> None:
    """user_requested=True (mensaje puntual señalado por el usuario) pasa siempre."""
    metadata = {
        "from": "desconocido@spam.com",
        "subject": "Cualquier cosa",
        "label_ids": ["INBOX"],
    }
    result = await gmail_preflight_check(
        metadata,
        approved_senders=frozenset(),
        vektor_label_ids=frozenset(),
        user_requested=True,
    )
    assert result is True
