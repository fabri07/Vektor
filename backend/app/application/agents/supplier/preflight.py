"""Pre-flight check de Gmail para AgentSupplier.

OBLIGATORIO: ejecutar antes de abrir el CUERPO de cualquier correo de Gmail.
Un correo pasa si AL MENOS UNA de las tres condiciones se cumple:
  1. El remitente es un proveedor aprobado del tenant.
  2. El correo tiene la label "Véktor" (o "Vektor" sin tilde).
  3. El usuario señaló explícitamente ESE mensaje puntual (``user_requested``).

``user_requested`` NO es "el usuario pidió revisar la bandeja" — aprobar una
revisión general de la bandeja no autoriza indiscriminadamente cualquier
correo. Solo aplica cuando el payload trae un ``message_id`` puntual que el
usuario señaló (ver ``gmail_inbox_review_service.review_gmail_inbox``).
"""

from __future__ import annotations

from email.utils import parseaddr
from typing import Any


def normalize_gmail_sender(raw: str) -> str:
    """Extrae y normaliza la dirección de un header ``From``.

    ``"Empresa S.A. <compras@proveedor.com>"`` → ``"compras@proveedor.com"``.
    Sin esto, un remitente aprobado por email nunca matchea un ``From`` real
    de Gmail, que casi siempre trae un nombre para mostrar.
    """
    _display_name, address = parseaddr(raw or "")
    return address.strip().lower()


async def gmail_preflight_check(
    metadata: dict[str, Any],
    *,
    approved_senders: frozenset[str],
    vektor_label_ids: frozenset[str],
    user_requested: bool = False,
) -> bool:
    """
    Retorna True solo si el correo está autorizado para ser procesado.

    metadata esperado (de ``get_message`` en modo ``metadata``): {
      "from": "Proveedor <email@proveedor.com>",
      "subject": "Lista de precios",
      "label_ids": ["INBOX", "Label_170..."],
    }

    ``approved_senders`` y ``vektor_label_ids`` se resuelven UNA vez por
    ejecución (no por mensaje) — ver ``review_gmail_inbox``.
    """
    sender = normalize_gmail_sender(metadata.get("from", ""))
    labels = metadata.get("label_ids") or metadata.get("labels") or []

    # Condición 1: sender registrado como proveedor
    if sender and sender in approved_senders:
        return True

    # Condición 2: tiene la label "Véktor"/"Vektor", resuelta a su id opaco
    if any(label_id in vektor_label_ids for label_id in labels):
        return True

    # Condición 3: el usuario señaló explícitamente este mensaje puntual
    return bool(user_requested)
