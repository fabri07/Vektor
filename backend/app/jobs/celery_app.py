"""
Celery application factory.

Queues:
  - default      : general tasks
  - scores       : health score recalculations
  - notifications: email / push notifications
  - reports      : scheduled report generation
  - ingestion    : file parsing jobs (spreadsheet, text, OCR)
"""

from celery import Celery

from app.config.settings import get_settings

settings = get_settings()

celery_app = Celery(
    "vektor",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=[
        "app.jobs.score_worker",
        "app.jobs.recalculate_health_score",
        "app.jobs.generate_insight",
        "app.jobs.notification_worker",
        "app.jobs.report_worker",
        "app.jobs.ingestion_worker",
        "app.application.services.score_trigger_service",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="America/Argentina/Buenos_Aires",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,           # re-queue on worker crash
    worker_prefetch_multiplier=1,  # one task at a time per worker
    task_routes={
        "jobs.trigger_score_recalculation": {"queue": "scores"},
        "jobs.recalculate_health_score": {"queue": "scores"},
        "jobs.generate_insight": {"queue": "scores"},
        "jobs.send_notification": {"queue": "notifications"},
        "jobs.generate_report": {"queue": "reports"},
        "jobs.process_spreadsheet": {"queue": "ingestion"},
        "jobs.process_text_document": {"queue": "ingestion"},
        "jobs.process_image_ocr": {"queue": "ingestion"},
    },
)

# ── Periodic tasks (Beat) ─────────────────────────────────────────────────────
celery_app.conf.beat_schedule = {
    "rebuild-weekly-score-history": {
        "task": "jobs.rebuild_weekly_history",
        "schedule": 60 * 60 * 24,  # daily at midnight
        "options": {"queue": "scores"},
    },
}
