#!/bin/sh
# Deja la base lista para la versión que está por recibir tráfico: esquema
# (Alembic) + datos de referencia derivados del código (definiciones de campos
# por rubro).
#
# Lo invoca Railway como preDeployCommand (ver railway.toml): corre UNA sola vez
# por deploy, en un contenedor one-off con el mismo image y env (DATABASE_URL →
# Neon en producción), ANTES de que la nueva versión reciba tráfico.
#
# Los dos pasos son idempotentes: sin migraciones pendientes `alembic upgrade
# head` es un no-op, y el seed es un upsert (UPDATE si existe, INSERT si no —
# nunca borra, y no toca `tenant_custom_field_definitions`, que son los overrides
# del tenant). Si cualquiera de los dos falla, `set -e` corta con exit != 0 y
# Railway aborta el deploy: la versión vieja sigue sirviendo (fail-safe).
set -eu

# Antes de migrar, dejar asentado en el log contra qué base se migra. No corta el
# deploy nunca (sale 0 aunque falle): la autoridad sigue siendo alembic.
python scripts/migrate_preflight.py

echo "[migrate] alembic upgrade head"
alembic upgrade head
echo "[migrate] OK"

# El seed va DESPUÉS de las migraciones: la primera vez que se agrega un rubro,
# la tabla que el seed escribe puede ser justamente la que acaba de crear o
# ensanchar una migración de este mismo deploy.
#
# Está acá y no como paso manual porque agregar un rubro toca dos lugares que se
# deployan juntos —el enum del código y su JSON de campos— y solo uno de los dos
# viajaba solo. Un rubro nuevo llegaba a producción sin sus campos curados: no
# rompe (la consulta devuelve lista vacía), pero el negocio importa planillas sin
# las columnas de su rubro y el fallo se ve como "a Véktor le falta algo",
# semanas después y lejos del deploy que lo causó.
echo "[migrate] seed vertical field definitions"
python scripts/seed_vertical_fields.py
echo "[migrate] seed OK"
