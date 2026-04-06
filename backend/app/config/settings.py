"""
Application settings loaded from environment variables.
All fields are documented with their purpose and defaults.
"""

import json
import re
from functools import lru_cache
from typing import ClassVar

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _to_asyncpg_url(raw: str) -> str:
    """Convert a standard postgres:// URL to asyncpg-compatible format.

    Strips params asyncpg doesn't understand (channel_binding, sslmode).
    SSL is handled separately via connect_args.
    """
    url = raw.replace("postgresql://", "postgresql+asyncpg://", 1)
    url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    for param in ("channel_binding", "sslmode"):
        url = re.sub(rf"[?&]{param}=[^&]*", "", url)
    url = re.sub(r"\?$", "", url)
    url = re.sub(r"\?&", "?", url)
    return url


def _to_psycopg2_url(raw: str) -> str:
    """Convert a standard postgres:// URL to psycopg2-compatible format."""
    url = raw.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
    url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
    url = url.replace("postgres://", "postgresql+psycopg2://", 1)
    # channel_binding not supported by psycopg2 — strip it, keep sslmode
    url = re.sub(r"[?&]channel_binding=[^&]*", "", url)
    url = re.sub(r"\?$", "", url)
    url = re.sub(r"\?&", "?", url)
    return url


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
    # Set DATABASE_URL directly (Railway/Neon style) OR use individual POSTGRES_* fields.
    DATABASE_URL_RAW: str | None = Field(
        default=None,
        validation_alias=AliasChoices("DATABASE_URL", "DATABASE_URL_RAW"),
    )
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
    # Accepts CORS_ORIGINS or ALLOWED_ORIGINS (Railway naming)
    CORS_ORIGINS: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            "http://localhost:3002",
            "http://127.0.0.1:3002",
        ],
        validation_alias=AliasChoices("CORS_ORIGINS", "ALLOWED_ORIGINS"),
    )

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
    # Accepts S3_ENDPOINT_URL or S3_ENDPOINT (Railway naming)
    S3_ENDPOINT_URL: str | None = Field(
        default=None,
        validation_alias=AliasChoices("S3_ENDPOINT_URL", "S3_ENDPOINT"),
    )
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

    # Auth social
    ENABLE_GOOGLE_LOGIN: bool = False
    ENABLE_FACEBOOK_LOGIN: bool = False  # Diferido — solo abstracción en fase 1

    # Google Workspace MCP gateway (herramientas locales Python)
    ENABLE_GOOGLE_WORKSPACE_MCP: bool = False

    # ── Google OAuth ──────────────────────────────────────────────────────────
    GOOGLE_OAUTH_CLIENT_ID: str = ""
    GOOGLE_OAUTH_CLIENT_SECRET: str = ""
    # Redirect URI para login social (Google Cloud Console)
    GOOGLE_OAUTH_REDIRECT_URI: str = "http://localhost:8000/api/v1/auth/oauth/google/callback"
    # Redirect URI para Workspace connect (debe registrarse por separado en Google Cloud Console)
    GOOGLE_WORKSPACE_REDIRECT_URI: str = "http://localhost:8000/api/v1/workspace/google/connect/callback"

    # Clave Fernet (URL-safe base64, 32 bytes) para cifrar tokens de Workspace.
    # Generar con: python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    GOOGLE_TOKEN_CIPHER_KEY: str = ""

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
            merged_origins = list(dict.fromkeys([*self.CORS_ORIGINS, *self.__class__.DEV_CORS_ORIGINS]))
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

        # ── Fail-fast: credenciales OAuth requeridas cuando los flags están activos ──
        # Se valida en cualquier entorno (no solo producción) para detectar
        # configuraciones rotas antes del primer request OAuth.
        if self.ENABLE_GOOGLE_LOGIN or self.ENABLE_GOOGLE_WORKSPACE_MCP:
            missing: list[str] = []
            if not self.GOOGLE_OAUTH_CLIENT_ID:
                missing.append("GOOGLE_OAUTH_CLIENT_ID")
            if not self.GOOGLE_OAUTH_CLIENT_SECRET:
                missing.append("GOOGLE_OAUTH_CLIENT_SECRET")
            if self.ENABLE_GOOGLE_LOGIN and not self.GOOGLE_OAUTH_REDIRECT_URI:
                missing.append("GOOGLE_OAUTH_REDIRECT_URI")
            if self.ENABLE_GOOGLE_WORKSPACE_MCP and not self.GOOGLE_WORKSPACE_REDIRECT_URI:
                missing.append("GOOGLE_WORKSPACE_REDIRECT_URI")
            if missing:
                flags = ", ".join(
                    f for f in ("ENABLE_GOOGLE_LOGIN", "ENABLE_GOOGLE_WORKSPACE_MCP")
                    if getattr(self, f)
                )
                raise ValueError(
                    f"Los flags [{flags}] están activos pero faltan las credenciales: "
                    f"{', '.join(missing)}"
                )

        if self.ENABLE_GOOGLE_WORKSPACE_MCP and not self.GOOGLE_TOKEN_CIPHER_KEY:
            raise ValueError(
                "ENABLE_GOOGLE_WORKSPACE_MCP está activo pero falta GOOGLE_TOKEN_CIPHER_KEY. "
                "Generá una con: "
                "python3 -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            )

        return self

    # ── Computed properties ───────────────────────────────────────────────────
    @property
    def DATABASE_URL(self) -> str:  # noqa: N802
        """Async database URL for SQLAlchemy (asyncpg).

        Uses DATABASE_URL_RAW (set via DATABASE_URL env var) if available,
        stripping params asyncpg doesn't support (channel_binding, sslmode).
        SSL is injected via pg_connect_args instead.
        """
        if self.DATABASE_URL_RAW:
            return _to_asyncpg_url(self.DATABASE_URL_RAW)
        return (
            f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def DATABASE_URL_SYNC(self) -> str:  # noqa: N802
        """Sync database URL for Alembic / Celery tasks (psycopg2)."""
        if self.DATABASE_URL_RAW:
            return _to_psycopg2_url(self.DATABASE_URL_RAW)
        return (
            f"postgresql+psycopg2://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def pg_connect_args(self) -> dict:  # type: ignore[type-arg]
        """SSL connect_args for asyncpg when DATABASE_URL requires SSL (Neon, RDS, etc.)."""
        if self.DATABASE_URL_RAW and "sslmode=require" in self.DATABASE_URL_RAW:
            import ssl  # noqa: PLC0415
            return {"ssl": ssl.create_default_context()}
        return {}

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
