"""Alembic environment — supports both online (sync) and offline modes."""

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

# Load all models so Alembic can detect schema changes
import app.persistence.models  # noqa: F401
from alembic import context
from app.persistence.db.alembic_url import resolve_sync_url
from app.persistence.db.base import Base

# Alembic Config object (access to alembic.ini values)
config = context.config

# Resolve sync DB URL: prefer DATABASE_URL_SYNC, fall back to DATABASE_URL (converted),
# then alembic.ini default. La resolución vive en `app.persistence.db.alembic_url`
# porque `scripts/migrate_preflight.py` tiene que resolver EXACTAMENTE lo mismo.
config.set_main_option(
    "sqlalchemy.url",
    resolve_sync_url(config.get_main_option("sqlalchemy.url")).url,
)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations without a live database connection (generates SQL)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


#: Clave del advisory lock que serializa `alembic upgrade`. Un número fijo y
#: propio: cualquier otro advisory lock del sistema usa claves distintas.
MIGRATION_LOCK_KEY = 7_411_300_921


def do_run_migrations(connection: object) -> None:
    context.configure(
        connection=connection,  # type: ignore[arg-type]
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        # Dos procesos pueden correr `alembic upgrade head` a la vez: Railway
        # despliega `vektor-api` y `vektor-worker` en paralelo y los dos ejecutan
        # `scripts/migrate.sh`. Pasó el 2026-09-24: los dos vieron que
        # `idempotency_records` no existía (los guards de E8c comprueban presencia,
        # no son atómicos), los dos la crearon, y el perdedor abortó su deploy con
        # `UniqueViolation` sobre `pg_type`.
        #
        # El lock se toma como PRIMERA sentencia de la transacción de migración,
        # antes de que alembic lea `alembic_version`: el segundo proceso espera,
        # y cuando entra lee la versión que el primero ya commiteó y no hace nada.
        # Es `xact` y no de sesión porque Neon se usa por el pooler en modo
        # transacción, donde un lock de sesión puede quedar en otra conexión
        # física; éste se suelta solo con el COMMIT/ROLLBACK.
        if connection.dialect.name == "postgresql":  # type: ignore[attr-defined]
            connection.exec_driver_sql(  # type: ignore[attr-defined]
                f"SELECT pg_advisory_xact_lock({MIGRATION_LOCK_KEY})"
            )
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations with a live database connection (sync via psycopg2)."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        do_run_migrations(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
