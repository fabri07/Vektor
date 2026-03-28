"""Alembic environment — supports both online (sync) and offline modes."""

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Load all models so Alembic can detect schema changes
import app.persistence.models  # noqa: F401
from app.persistence.db.base import Base

# Alembic Config object (access to alembic.ini values)
config = context.config

# Resolve sync DB URL: prefer DATABASE_URL_SYNC, fall back to DATABASE_URL (converted),
# then alembic.ini default.
def _resolve_sync_url() -> str:
    if url := os.environ.get("DATABASE_URL_SYNC"):
        return url
    if raw := os.environ.get("DATABASE_URL"):
        # Convert postgresql:// → postgresql+psycopg2://, strip channel_binding
        import re  # noqa: PLC0415
        url = raw.replace("postgresql://", "postgresql+psycopg2://", 1)
        url = url.replace("postgres://", "postgresql+psycopg2://", 1)
        url = re.sub(r"[?&]channel_binding=[^&]*", "", url)
        url = re.sub(r"\?$", "", url)
        url = re.sub(r"\?&", "?", url)
        return url
    return config.get_main_option("sqlalchemy.url") or ""

config.set_main_option("sqlalchemy.url", _resolve_sync_url())

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


def do_run_migrations(connection: object) -> None:
    context.configure(
        connection=connection,  # type: ignore[arg-type]
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
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
