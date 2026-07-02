"""CoverageGapService — registra los rechazos del chat como backlog de producto.

Cada consulta que el chat rechaza o no cubre (fuera de scope, intent
desconocido, baja confianza, sin datos, ui_context faltante) se persiste en
`chat_coverage_gaps` para revisión posterior: es la señal de qué piden los
usuarios que Véktor todavía no resuelve.

Contrato duro: **best-effort, NUNCA bloqueante**. `log_gap()` no lanza jamás
y escribe en una sesión propia (`async_session_factory`) — un INSERT fallido
no puede envenenar la transacción del request ni demorar la respuesta al
usuario. Mismo espíritu fail-silent que las capas de memoria del orchestrator.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.observability.logger import get_logger
from app.persistence.db.session import async_session_factory
from app.persistence.models.coverage_gap import COVERAGE_GAP_REASONS, ChatCoverageGap

logger = get_logger(__name__)

# El mensaje se trunca para que un paste gigante no infle la tabla de backlog.
_MAX_MESSAGE_LEN = 4000

# ui_context viene del cliente sin validación de forma: se persiste SOLO el
# subset conocido, con cotas — mismo invariante que la truncación del mensaje.
_UI_STR_KEYS = ("view", "focused_widget")
_UI_LIST_KEYS = ("active_alert_ids", "visible_metric_ids")
_MAX_UI_STR_LEN = 100
_MAX_UI_LIST_ITEMS = 16
_MAX_UI_ITEM_LEN = 64


def _sanitize_ui_context(ui_context: Any) -> dict[str, Any] | None:
    """Allowlist + cotas sobre el dict del cliente. Cualquier forma rara → None."""
    if not isinstance(ui_context, dict):
        return None
    out: dict[str, Any] = {}
    for key in _UI_STR_KEYS:
        value = ui_context.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = value[:_MAX_UI_STR_LEN]
    for key in _UI_LIST_KEYS:
        value = ui_context.get(key)
        if isinstance(value, list):
            items = [
                str(v)[:_MAX_UI_ITEM_LEN]
                for v in value[:_MAX_UI_LIST_ITEMS]
                if isinstance(v, str | int)
            ]
            if items:
                out[key] = items
    return out or None


def reason_for_intent(intent: str | None) -> str:
    """Traducción única intent→fallback_reason para los cortes del orchestrator.

    out_of_scope/intent_desconocido se registran literal; cualquier otro corte
    (pedir_aclaracion_*, gate de confianza) es `baja_confianza`. Vive acá, al
    lado del set cerrado, para que un intent de corte nuevo no invente una
    reason fuera del CheckConstraint.
    """
    if intent in ("out_of_scope", "intent_desconocido"):
        return intent
    return "baja_confianza"


class CoverageGapService:
    """Escritor insert-only de gaps de cobertura. Sesión propia por escritura."""

    async def log_gap(
        self,
        *,
        tenant_id: uuid.UUID,
        original_message: str,
        fallback_reason: str,
        user_id: uuid.UUID | None = None,
        classified_intent: str | None = None,
        classified_domain: str | None = None,
        confidence: float | None = None,
        ui_context: dict[str, Any] | None = None,
    ) -> None:
        """Registra un gap. Traga CUALQUIER error: el chat sigue como si nada."""
        try:
            if fallback_reason not in COVERAGE_GAP_REASONS:
                logger.warning(
                    "coverage_gap_unknown_reason",
                    reason=fallback_reason,
                    tenant_id=str(tenant_id),
                )
                return
            async with async_session_factory() as session:
                session.add(
                    ChatCoverageGap(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        original_message=(original_message or "")[:_MAX_MESSAGE_LEN],
                        classified_intent=classified_intent,
                        classified_domain=classified_domain,
                        confidence=confidence,
                        fallback_reason=fallback_reason,
                        ui_context=_sanitize_ui_context(ui_context),
                    )
                )
                await session.commit()
        except Exception as exc:
            logger.warning(
                "coverage_gap_log_failed",
                error=str(exc),
                reason=fallback_reason,
                tenant_id=str(tenant_id),
            )
