#!/bin/sh
set -eu

: "${PORT:=8000}"
: "${UVICORN_WORKERS:=1}"

# Las migraciones ahora corren en el preDeployCommand de Railway (scripts/migrate.sh),
# una sola vez por deploy en vez de por instancia. No las corremos acá.

exec uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --workers "$UVICORN_WORKERS"
