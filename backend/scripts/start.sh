#!/bin/sh
set -eu

runtime="${VEKTOR_RUNTIME:-${SERVICE_TYPE:-}}"
service_name="${RAILWAY_SERVICE_NAME:-}"

case "$(printf '%s %s' "$runtime" "$service_name" | tr '[:upper:]' '[:lower:]')" in
  *beat*)
    exec sh scripts/start_beat.sh
    ;;
  *worker*)
    exec sh scripts/start_worker.sh
    ;;
  *)
    exec sh scripts/start_web.sh
    ;;
esac
