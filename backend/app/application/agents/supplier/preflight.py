"""Pre-flight check de Gmail para AgentSupplier.

OBLIGATORIO: ejecutar antes de abrir cualquier correo de Gmail.
Un correo pasa si AL MENOS UNA de las tres condiciones se cumple.
Si ninguna se cumple → GMAIL_SKIPPED y se registra en audit_log.
"""

from app.application.services.supplier_service import get_approved_senders


async def gmail_preflight_check(
    metadata: dict,
    business_id: str,
    db=None,
    user_requested: bool = False,
) -> bool:
    """
    Retorna True solo si el correo está autorizado para ser procesado.

    metadata esperado: {
      "from": "email@proveedor.com",
      "subject": "Lista de precios",
      "labels": ["INBOX", "Véktor"],
      "snippet": "..."
    }
    """
    sender = metadata.get("from", "").lower()
    labels = metadata.get("labels", [])

    # Condición 1: sender registrado como proveedor
    if db:
        approved_senders = await get_approved_senders(business_id, db)
        if sender in [s.lower() for s in approved_senders]:
            return True

    # Condición 2: tiene label "Véktor" (o "Vektor" sin tilde)
    if "Véktor" in labels or "Vektor" in labels:
        return True

    # Condición 3: usuario lo solicitó explícitamente
    if user_requested:
        return True

    return False  # → GMAIL_SKIPPED


async def preflight_and_log(
    metadata: dict,
    business_id: str,
    db,
    audit_logger,
    user_requested: bool = False,
) -> bool:
    """Versión con audit logging automático."""
    result = await gmail_preflight_check(metadata, business_id, db, user_requested)
    if not result:
        await audit_logger.log(
            business_id=business_id,
            action="GMAIL_SKIPPED",
            details={
                "sender": metadata.get("from"),
                "subject": metadata.get("subject"),
            },
        )
    return result
