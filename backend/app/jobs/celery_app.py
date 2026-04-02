"""
Celery application factory.

Queues:
  - default      : general tasks
  - scores       : health score recalculations
  - notifications: email / push notifications
  - reports      : scheduled report generation
  - ingestion    : file parsing jobs (spreadsheet, text, OCR)
"""

import ssl

from celery import Celery
from celery.schedules import crontab as _crontab

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
        "app.jobs.update_momentum",
        "app.jobs.send_weekly_email",
        "app.application.services.score_trigger_service",
        "app.jobs.stock_tasks",
    ],
)

# ── SSL for rediss:// (Upstash / TLS-enabled Redis) ──────────────────────────
_ssl_opts = {"ssl_cert_reqs": ssl.CERT_NONE}
if settings.CELERY_BROKER_URL.startswith("rediss://"):
    celery_app.conf.broker_use_ssl = _ssl_opts
if settings.CELERY_RESULT_BACKEND.startswith("rediss://"):
    celery_app.conf.redis_backend_use_ssl = _ssl_opts

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
        "jobs.update_momentum_profile": {"queue": "scores"},
        "jobs.update_momentum_all_tenants": {"queue": "scores"},
        "jobs.send_notification": {"queue": "notifications"},
        "jobs.send_weekly_email_summary": {"queue": "notifications"},
        "jobs.send_weekly_email_all_tenants": {"queue": "notifications"},
        "jobs.generate_report": {"queue": "reports"},
        "jobs.process_spreadsheet": {"queue": "ingestion"},
        "jobs.process_text_document": {"queue": "ingestion"},
        "jobs.process_image_ocr": {"queue": "ingestion"},
    },
)

# ── Periodic tasks (Beat) ─────────────────────────────────────────────────────
# TODO: implementar scheduler por tenant usando weekly_report_day
# y weekly_report_hour de business_profiles. v1: todos los tenants corren
# el lunes a las 08:00 ART (crontab hour=8, day_of_week=1).
celery_app.conf.beat_schedule = {
    "rebuild-weekly-score-history": {
        "task": "jobs.rebuild_weekly_history",
        "schedule": 60 * 60 * 24,  # daily at midnight
        "options": {"queue": "scores"},
    },
    "update-momentum-all-tenants": {
        "task": "jobs.update_momentum_all_tenants",
        # Every Monday at 08:00 ART (UTC-3 → 11:00 UTC). Using crontab-style
        # expressed as seconds: run via crontab from celery.schedules.
        "schedule": _crontab(hour=11, minute=0, day_of_week=1),
        "options": {"queue": "scores"},
    },
    "send-weekly-email-all-tenants": {
        "task": "jobs.send_weekly_email_all_tenants",
        # Every Monday at 08:30 ART (UTC-3 → 11:30 UTC), after momentum update.
        "schedule": _crontab(hour=11, minute=30, day_of_week=1),
        "options": {"queue": "notifications"},
    },
}
