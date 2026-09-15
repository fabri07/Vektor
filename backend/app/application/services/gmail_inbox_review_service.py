"""gmail_inbox_review_service.py — revisión autorizada de la bandeja de Gmail.

Único punto que orquesta: listar candidatos → resolver metadata → preflight
→ leer el contenido de los autorizados. Reemplaza el flujo que quedó
huérfano al migrar de la integración propia de Google Workspace al MCP
server (ver ``AgentSupplier._handle_classify_inbox``).

No clasifica el contenido ni genera borradores de respuesta — eso es una
capacidad aparte, todavía no reconstruida sobre MCP.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.application.agents.supplier.preflight import gmail_preflight_check
from app.application.services.supplier_service import get_approved_senders
from app.integrations.mcp.exceptions import McpToolAuthError
from app.observability.logger import get_logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.application.agents.google.tool_broker import GoogleToolBroker

logger = get_logger(__name__)

_VEKTOR_LABEL_NAMES = frozenset({"véktor", "vektor"})
DEFAULT_MAX_RESULTS = 10


class GmailReviewRequestError(ValueError):
    """El payload de CLASSIFY_GMAIL_MESSAGE no trae ni ``query`` ni ``message_id``."""


@dataclass
class GmailReviewResult:
    disabled: bool = False
    candidates: int = 0
    authorized: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    failed: int = 0
    has_more: bool = False
    message: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "disabled": self.disabled,
            "candidates": self.candidates,
            "authorized_count": len(self.authorized),
            "authorized": self.authorized,
            "skipped_count": len(self.skipped),
            "skipped": self.skipped,
            "failed": self.failed,
            "has_more": self.has_more,
            "message": self.message,
        }


async def _resolve_vektor_label_ids(broker: GoogleToolBroker) -> frozenset[str]:
    try:
        labels = await broker.list_gmail_labels()
    except McpToolAuthError:
        raise
    except Exception:
        logger.warning("gmail_review.list_labels_failed")
        return frozenset()
    return frozenset(
        str(lbl.get("id"))
        for lbl in labels
        if str(lbl.get("name", "")).strip().lower() in _VEKTOR_LABEL_NAMES and lbl.get("id")
    )


async def review_gmail_inbox(
    *,
    broker: GoogleToolBroker,
    db: AsyncSession,
    tenant_id: uuid.UUID,
    query: str | None,
    message_id: str | None,
    max_results: int = DEFAULT_MAX_RESULTS,
) -> GmailReviewResult:
    """Ejecuta UNA página de revisión de bandeja.

    - ``message_id`` (si viene, no vacío): revisa ESE mensaje puntual — el
      usuario lo señaló explícitamente, así que el preflight corre con
      ``user_requested=True``.
    - ``query`` (si ``message_id`` no viene): lista candidatos de la bandeja
      y aplica preflight con ``user_requested=False`` — revisar la bandeja
      general NO autoriza indiscriminadamente cualquier correo, solo
      remitentes aprobados o la label "Véktor"/"Vektor".
    - Ninguno de los dos presente → ``GmailReviewRequestError``.
    - Procesa UNA sola página: no sigue ``next_page_token`` automáticamente.
      ``has_more`` en el resultado indica si quedaron candidatos sin revisar.
    """
    if not message_id and not query:
        raise GmailReviewRequestError("Falta 'query' o 'message_id' en el payload.")

    result = GmailReviewResult()

    explicit = bool(message_id)
    candidate_ids: list[str]
    if message_id:
        candidate_ids = [message_id]
    else:
        assert query is not None
        page = await broker.list_gmail_messages(query=query, max_results=max_results)
        if page.disabled:
            result.disabled = True
            result.message = "La integración de Gmail no está habilitada."
            return result
        candidate_ids = page.message_ids
        result.has_more = bool(page.next_page_token)

    result.candidates = len(candidate_ids)
    if result.candidates == 0:
        result.message = "No encontré mensajes nuevos en la bandeja."
        return result

    approved_senders = frozenset(s.lower() for s in await get_approved_senders(tenant_id, db))
    vektor_label_ids = await _resolve_vektor_label_ids(broker)

    for mid in candidate_ids:
        try:
            meta = await broker.get_gmail_message(mid, msg_format="metadata")
        except McpToolAuthError:
            # No es "este mensaje falló": la cuenta perdió el permiso/conexión.
            # Todo lo demás va a fallar igual — no lo escondemos como conteo.
            raise
        except Exception:
            logger.warning(
                "gmail_review.metadata_failed", message_id=mid, tenant_id=str(tenant_id)
            )
            result.failed += 1
            continue

        passes = await gmail_preflight_check(
            meta,
            approved_senders=approved_senders,
            vektor_label_ids=vektor_label_ids,
            user_requested=explicit,
        )
        if not passes:
            result.skipped.append(
                {
                    "message_id": mid,
                    "from": meta.get("from", ""),
                    "subject": meta.get("subject", ""),
                }
            )
            continue

        try:
            full = await broker.get_gmail_message(mid, msg_format="full")
        except McpToolAuthError:
            raise
        except Exception:
            logger.warning("gmail_review.fetch_failed", message_id=mid, tenant_id=str(tenant_id))
            result.failed += 1
            continue

        result.authorized.append(
            {
                "message_id": mid,
                "from": full.get("from", ""),
                "subject": full.get("subject", ""),
                "date": full.get("date", ""),
            }
        )

    result.message = (
        f"Revisé {result.candidates} mensaje(s): {len(result.authorized)} de proveedores "
        f"conocidos, {len(result.skipped)} filtrado(s), {result.failed} con error de lectura."
    )
    return result
