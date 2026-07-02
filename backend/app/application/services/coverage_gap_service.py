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
                        ui_context=ui_context,
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
