from typing import Any

from celery import current_app as celery_app
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession


class EventBus:
    @staticmethod
    def emit(event_type: str, payload: dict[str, Any]) -> None:
        """Emitir un evento vía Celery task.

        **Encolar nunca puede tumbar la operación que lo emitió.** Un evento del
        bus es un backstop asíncrono: si el broker no está, se pierde el aviso y
        el estado en la base sigue siendo el correcto. Dejar propagar la excepción
        hace lo contrario — con `decrement_stock` el error sale por el `commit()`
        del endpoint y devuelve un 500 por un descuento de inventario que sí se
        aplicó, invitando al usuario a repetirlo. Es la misma regla que
        ``trigger_score_recalculation_after_commit`` documenta como obligatoria, y
        vale para los DOS caminos de emisión porque `emit_after_commit` termina
        acá.
        """
        try:
            celery_app.send_task(f"events.{event_type.lower()}", kwargs={"payload": payload})
        except Exception:  # noqa: BLE001 — un evento nunca rompe la respuesta
            from app.observability.logger import get_logger  # noqa: PLC0415

            get_logger(__name__).warning(
                "event_bus.emit_failed",
                event_type=event_type,
            )

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

        Acá el fail-safe de ``emit`` no es una comodidad sino un requisito: este
        listener corre DESPUÉS del commit, así que una excepción suya sale por el
        `commit()` del endpoint cuando ya no hay nada que revertir.
        """
        sync_session = db.sync_session

        @event.listens_for(sync_session, "after_commit", once=True)
        def _emit(_session: Any) -> None:
            EventBus.emit(event_type, payload)
