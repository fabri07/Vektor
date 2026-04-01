from celery import current_app as celery_app


class EventBus:
    @staticmethod
    def emit(event_type: str, payload: dict) -> None:
        """Emitir un evento vía Celery task."""
        celery_app.send_task(
            f"events.{event_type.lower()}",
            kwargs={"payload": payload}
        )
