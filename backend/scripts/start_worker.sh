#!/bin/sh
set -eu

: "${CELERY_QUEUES:=default,scores,notifications,reports,ingestion}"
: "${CELERY_CONCURRENCY:=2}"
: "${CELERY_LOGLEVEL:=info}"
: "${WORKER_HEALTHCHECK_SERVER:=true}"

if [ "$WORKER_HEALTHCHECK_SERVER" = "true" ] && [ -n "${PORT:-}" ]; then
  python scripts/worker_healthcheck.py &
fi

exec celery -A app.jobs.celery_app worker \
  --loglevel="$CELERY_LOGLEVEL" \
  --queues="$CELERY_QUEUES" \
  --concurrency="$CELERY_CONCURRENCY"
