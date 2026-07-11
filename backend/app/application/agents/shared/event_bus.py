from typing import Any

from celery import current_app as celery_app
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession


class EventBus:
    @staticmethod
    def emit(event_type: str, payload: dict[str, Any]) -> None:
        """Emitir un evento vía Celery task."""
        celery_app.send_task(f"events.{event_type.lower()}", kwargs={"payload": payload})

    @staticmethod
    def emit_after_commit(
        db: AsyncSession, event_type: str, payload: dict[str, Any]
    ) -> None:
        """Emitir el evento SOLO después de que la transacción del request commitee.

        Necesario cuando un handler async reacciona leyendo de la DB lo que escribió
        el request (ej. ``events.sale_recorded`` lee el ``SaleEntry``): si el task
        corre ANTES del commit, no encuentra la fila y termina sin reintentar. Emitir
        en el ``after_commit`` de la sesión garantiza que el backstop siempre vea el
        estado commiteado. Si la transacción hace rollback, el listener nunca dispara.
        """
        sync_session = db.sync_session

        @event.listens_for(sync_session, "after_commit", once=True)
        def _emit(_session: Any) -> None:
            EventBus.emit(event_type, payload)
