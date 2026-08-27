"""
Celery application factory.

Queues:
  - default      : general tasks
  - scores       : health score recalculations
  - notifications: email / push notifications
  - reports      : scheduled report generation
  - ingestion    : file parsing jobs (spreadsheet, text, OCR)
"""

import inspect
import ssl
from typing import Any

from celery import Celery
from celery.schedules import crontab as _crontab
from celery.signals import beat_init, celeryd_init, task_postrun, task_prerun

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
        "app.jobs.contact_lead_worker",
        "app.jobs.access_request_worker",
        "app.jobs.report_worker",
        "app.jobs.ingestion_worker",
        "app.jobs.reread_worker",
        "app.jobs.reread_sweep_worker",
        "app.jobs.update_momentum",
        "app.jobs.send_weekly_email",
        "app.application.services.score_trigger_service",
        "app.jobs.stock_tasks",
        "app.jobs.inventory_integrity_check",
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
    task_acks_late=True,  # re-queue on worker crash
    task_time_limit=300,
    task_soft_time_limit=240,
    worker_prefetch_multiplier=1,  # one task at a time per worker
    task_routes={
        "jobs.trigger_score_recalculation": {"queue": "scores"},
        "jobs.recalculate_health_score": {"queue": "scores"},
        "jobs.generate_insight": {"queue": "scores"},
        "jobs.update_momentum_profile": {"queue": "scores"},
        "jobs.update_momentum_all_tenants": {"queue": "scores"},
        "jobs.send_notification": {"queue": "notifications"},
        # Los cuatro emails del flujo de solicitudes de acceso. El nombre de la
        # última no espeja al de su función (ver access_request_worker).
        "jobs.notify_access_request_verification": {"queue": "notifications"},
        "jobs.notify_access_request_owner": {"queue": "notifications"},
        "jobs.notify_access_request_decision": {"queue": "notifications"},
        "jobs.notify_account_exists": {"queue": "notifications"},
        "jobs.send_weekly_email_summary": {"queue": "notifications"},
        "jobs.send_weekly_email_all_tenants": {"queue": "notifications"},
        "jobs.generate_report": {"queue": "reports"},
        "jobs.process_spreadsheet": {"queue": "ingestion"},
        "jobs.process_text_document": {"queue": "ingestion"},
        "jobs.process_image_ocr": {"queue": "ingestion"},
        "jobs.reread_apply": {"queue": "ingestion"},
        # F-RR (hallazgo de code review): el sweep es un AUDITOR de la cola
        # `ingestion` — si nadie la consume (el incidente real de ASTERIA), un
        # sweep que vive en esa misma cola tampoco corre, y el housekeeping que
        # debería recuperar los runs huérfanos queda igual de trabado que ellos.
        # Mismo criterio que `inventory_integrity_check` (también un auditor
        # periódico, no la carga primaria que audita): vive en `scores`.
        "jobs.sweep_stale_reread_runs": {"queue": "scores"},
        "jobs.inventory_integrity_check": {"queue": "scores"},
        "jobs.inventory_integrity_check_all_tenants": {"queue": "scores"},
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
    "inventory-integrity-check-all-tenants": {
        "task": "jobs.inventory_integrity_check_all_tenants",
        # Every Wednesday at 03:00 ART (UTC-3 → 06:00 UTC) — semanal, fuera del
        # horario del lunes (momentum/email) ya ocupado; no es una alerta
        # accionable el mismo día, no necesita cadencia diaria.
        "schedule": _crontab(hour=6, minute=0, day_of_week=3),
        "options": {"queue": "scores"},
    },
    "sweep-stale-reread-runs": {
        "task": "jobs.sweep_stale_reread_runs",
        # F-RR Fase 5: housekeeping de sesiones/jobs de relectura colgados que
        # nadie reintentó — cada 10 min, independiente del guard reactivo
        # (que solo limpia cuando alguien vuelve a tocar ESE archivo/tenant).
        # Cola `scores` a propósito, no `ingestion` — ver el comentario en
        # `task_routes` de más arriba.
        "schedule": 10 * 60,
        "options": {"queue": "scores"},
    },
}


# ── Sentry ────────────────────────────────────────────────────────────────────
# Init por señal, NUNCA a nivel de módulo: este archivo también se importa
# desde el proceso web (para encolar tasks vía `.delay()`) — un init a nivel
# de módulo pisaría el Sentry del proceso web (tag service="web") en cada
# import. `celeryd_init`/`beat_init` solo disparan cuando el proceso arranca
# de verdad como `celery worker`/`celery beat`.


@celeryd_init.connect  # type: ignore[misc]
def _init_sentry_worker(**kwargs: object) -> None:
    from app.observability.sentry import init_sentry  # noqa: PLC0415

    init_sentry("worker")


@beat_init.connect  # type: ignore[misc]
def _init_sentry_beat(**kwargs: object) -> None:
    from app.observability.sentry import init_sentry  # noqa: PLC0415

    init_sentry("beat")


def _extract_tenant_id(task: object, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any | None:
    """
    Busca `tenant_id` por NOMBRE de parámetro, nunca por posición fija: tasks
    como `notify_access_request_account_exists(self, email)` no tienen
    `tenant_id`, y una regla "primer argumento posicional" les taggearía el
    error con el email de un usuario (PII, y encima mal etiquetado como
    tenant_id). `task.run` es un bound method (celery ya excluye `self` de la
    firma) que refleja exactamente los args/kwargs que recibió `.delay(...)`.
    """
    if "tenant_id" in kwargs:
        return kwargs["tenant_id"]

    run = getattr(task, "run", None)
    if run is None:
        return None
    try:
        params = list(inspect.signature(run).parameters)
    except (TypeError, ValueError):
        return None
    if "tenant_id" not in params:
        return None
    index = params.index("tenant_id")
    return args[index] if index < len(args) else None


@task_prerun.connect  # type: ignore[misc]
def _sentry_tag_task(
    task_id: str,
    task: object,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    **_: object,
) -> None:
    """
    Tag de negocio por task — el SDK aísla el scope de tracing/spans por task
    solo, pero no puede inferir `tenant_id`. `worker_prefetch_multiplier=1`
    hace que un mismo proceso corra tasks de tenants distintos en secuencia:
    sin limpiar el tag en `task_postrun`, la task N podría heredar el
    `tenant_id` de la task N-1.
    """
    import sentry_sdk  # noqa: PLC0415

    task_name = getattr(task, "name", "unknown")
    sentry_sdk.set_context("celery_task", {"name": task_name, "task_id": task_id})
    tenant_id = _extract_tenant_id(task, args, kwargs)
    if tenant_id is not None:
        sentry_sdk.set_tag("tenant_id", str(tenant_id))


@task_postrun.connect  # type: ignore[misc]
def _sentry_clear_task_tag(**kwargs: object) -> None:
    import sentry_sdk  # noqa: PLC0415

    sentry_sdk.set_tag("tenant_id", None)
