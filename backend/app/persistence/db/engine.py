"""
Async SQLAlchemy engine — one shared instance per process.
"""

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.config.settings import get_settings

settings = get_settings()

engine: AsyncEngine = create_async_engine(
    settings.DATABASE_URL,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,          # detect stale connections
    pool_recycle=3600,           # recycle connections every hour
    echo=settings.DEBUG,
    connect_args=settings.pg_connect_args,
)
