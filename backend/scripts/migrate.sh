#!/bin/sh
# Aplica las migraciones Alembic pendientes contra la DB del entorno.
#
# Lo invoca Railway como preDeployCommand (ver railway.toml): corre UNA sola vez
# por deploy, en un contenedor one-off con el mismo image y env (DATABASE_URL →
# Neon en producción), ANTES de que la nueva versión reciba tráfico.
#
# Es idempotente: si no hay migraciones pendientes, `alembic upgrade head` es un
# no-op. Si una migración falla, `set -e` corta con exit != 0 y Railway aborta el
# deploy — la versión vieja sigue sirviendo (fail-safe).
set -eu

echo "[migrate] alembic upgrade head"
alembic upgrade head
echo "[migrate] OK"
