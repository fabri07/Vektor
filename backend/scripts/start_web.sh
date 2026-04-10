#!/bin/sh
set -eu

: "${PORT:=8000}"
: "${UVICORN_WORKERS:=1}"

exec uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --workers "$UVICORN_WORKERS"
