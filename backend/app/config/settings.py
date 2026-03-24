"""
Application settings loaded from environment variables.
All fields are documented with their purpose and defaults.
"""

import json
from functools import lru_cache
from typing import ClassVar

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    DEV_CORS_ORIGINS: ClassVar[list[str]] = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:3002",
        "http://127.0.0.1:3002",
    ]

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # ── Application ───────────────────────────────────────────────────────────
    ENVIRONMENT: str = "development"
    DEBUG: bool = False
    SECRET_KEY: str = "insecure-change-me"

    # ── PostgreSQL ────────────────────────────────────────────────────────────
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "vektor"
    POSTGRES_USER: str = "vektor"
    POSTGRES_PASSWORD: str = "vektor"

    # ── Redis ─────────────────────────────────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"

    # ── Celery ────────────────────────────────────────────────────────────────
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"

    # ── JWT ───────────────────────────────────────────────────────────────────
    JWT_SECRET_KEY: str = "insecure-jwt-change-me"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440  # 24 hours
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # ── CORS ──────────────────────────────────────────────────────────────────
    CORS_ORIGINS: list[str] = DEV_CORS_ORIGINS.copy()

    @field_validator("DEBUG", mode="before")
    @classmethod
    def parse_debug(cls, v: bool | str) -> bool | str:
        if isinstance(v, str):
            normalized = v.strip().lower()
            if normalized in {"1", "true", "yes", "on", "debug"}:
                return True
            if normalized in {"0", "false", "no", "off", "release", "prod", "production"}:
                return False
        return v

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def parse_cors_origins(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            stripped = v.strip()
            if stripped.startswith("["):
                parsed = json.loads(stripped)
                if isinstance(parsed, list):
                    return [str(origin).strip() for origin in parsed]
            return [origin.strip() for origin in stripped.split(",")]
        return v

    # ── S3 Compatible Storage ─────────────────────────────────────────────────
    S3_ENDPOINT_URL: str | None = None
    S3_ACCESS_KEY_ID: str = ""
    S3_SECRET_ACCESS_KEY: str = ""
    S3_BUCKET_NAME: str = "vektor-uploads"
    S3_REGION: str = "us-east-1"

    # ── SMTP ──────────────────────────────────────────────────────────────────
    SMTP_HOST: str = "localhost"
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM_EMAIL: str = "noreply@vektor.app"
    SMTP_USE_TLS: bool = True

    # ── Feature flags ─────────────────────────────────────────────────────────
    ENABLE_SCORE_RECALCULATION: bool = True
    ENABLE_EMAIL_NOTIFICATIONS: bool = False
    ENABLE_EMAIL_VERIFICATION: bool = True
    SCORE_RECALC_COOLDOWN_SECONDS: int = 300

    # ── Demo mode ─────────────────────────────────────────────────────────────
    # Set DEMO_MODE=true to pre-load a kiosco tenant with realistic sample data.
    # In demo mode email verification is always skipped.
    DEMO_MODE: bool = False
    DEMO_EMAIL: str = "demo@vektor.app"
    DEMO_PASSWORD: str = "demo1234!"

    # ── Frontend ──────────────────────────────────────────────────────────────
    FRONTEND_URL: str = "http://localhost:3000"

    # ── OCR ───────────────────────────────────────────────────────────────────
    OCR_BACKEND: str = "tesseract"  # "tesseract" | "api" (future: external OCR API)

    # ── Production secret validation ──────────────────────────────────────────
    @model_validator(mode="after")
    def validate_production_secrets(self) -> "Settings":
        if self.is_development:
            merged_origins = list(dict.fromkeys([*self.CORS_ORIGINS, *self.DEV_CORS_ORIGINS]))
            self.CORS_ORIGINS = merged_origins

        if self.DEBUG or self.DEMO_MODE:
            # In debug/demo mode, skip email verification so local testing isn't blocked.
            self.ENABLE_EMAIL_VERIFICATION = False

        if self.ENVIRONMENT == "production":
            insecure_defaults = {"insecure-change-me", "insecure-jwt-change-me", ""}
            if self.SECRET_KEY in insecure_defaults or len(self.SECRET_KEY) < 32:
                raise ValueError(
                    "SECRET_KEY must be at least 32 characters and not a default value in production."
                )
            if self.JWT_SECRET_KEY in insecure_defaults or len(self.JWT_SECRET_KEY) < 32:
                raise ValueError(
                    "JWT_SECRET_KEY must be at least 32 characters and not a default value in production."
                )
        return self

    # ── Computed properties ───────────────────────────────────────────────────
    @property
    def DATABASE_URL(self) -> str:  # noqa: N802
        """Async database URL for SQLAlchemy (asyncpg)."""
        return (
            f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def DATABASE_URL_SYNC(self) -> str:  # noqa: N802
        """Sync database URL for Alembic / Celery tasks (psycopg2)."""
        return (
            f"postgresql+psycopg2://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"

    @property
    def is_development(self) -> bool:
        return self.ENVIRONMENT == "development"


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton. Loaded once at startup."""
    return Settings()
