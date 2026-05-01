#!/bin/sh
set -eu

: "${CELERY_LOGLEVEL:=info}"

exec celery -A app.jobs.celery_app beat --loglevel="$CELERY_LOGLEVEL"
