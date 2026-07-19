"""
Application settings loaded from environment variables.
All fields are documented with their purpose and defaults.
"""

import json
import re
from functools import lru_cache
from typing import ClassVar

from pydantic import AliasChoices, Field, ValidationInfo, field_validator, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, EnvSettingsSource, SettingsConfigDict


class _LenientEnvSource(EnvSettingsSource):
    """EnvSettingsSource that falls back to the raw string when JSON decoding fails.

    pydantic_settings 2.6 calls json.loads() on list/dict fields before our
    field_validators run. This subclass returns the raw value on decode failure
    so our validators (e.g. parse_cors_origins) can handle plain URLs and
    comma-separated strings without crashing the application startup.
    """

    def decode_complex_value(self, field_name: str, field: FieldInfo, value: object) -> object:
        try:
            return super().decode_complex_value(field_name, field, value)
        except ValueError:
            return value


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
        env_ignore_empty=True,
    )

    # ── Application ───────────────────────────────────────────────────────────
    ENVIRONMENT: str = "development"
    DEBUG: bool = Field(
        default=False,
        validation_alias=AliasChoices("APP_DEBUG", "VEKTOR_DEBUG"),
    )
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
    # Timeouts de asyncpg (segundos). Acotan conexión y cada query para que un
    # stall de Neon falle ruidoso en vez de colgar la task hasta el SIGKILL.
    PG_CONNECT_TIMEOUT: int = 10
    PG_COMMAND_TIMEOUT: int = 60

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
    # Accepts plain URL, comma-separated list, or JSON array.
    # _lenient_json_loads in model_config prevents pydantic_settings from crashing
    # when the value is not a JSON array (e.g. "https://vek7or.com").
    CORS_ORIGINS: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            "http://localhost:3002",
            "http://127.0.0.1:3002",
        ],
        validation_alias=AliasChoices("CORS_ORIGINS", "ALLOWED_ORIGINS"),
    )
    CORS_ORIGIN_REGEX: str | None = Field(
        default=None,
        validation_alias=AliasChoices("CORS_ORIGIN_REGEX", "ALLOWED_ORIGIN_REGEX"),
    )

    @field_validator(
        "DEBUG",
        "ENABLE_EMAIL_VERIFICATION",
        "ENABLE_EMAIL_NOTIFICATIONS",
        "ENABLE_SCORE_RECALCULATION",
        "ENABLE_GOOGLE_LOGIN",
        "ENABLE_FACEBOOK_LOGIN",
        "ENABLE_GOOGLE_MCP_TOOLS",
        "ENABLE_AGENT_AUTOMATIONS",
        "ENABLE_WHATSAPP",
        "ENABLE_SOCIAL_SYNC",
        "DEMO_MODE",
        "USE_LOCAL_FALLBACK",
        mode="before",
    )
    @classmethod
    def parse_bool_flags(cls, v: bool | str, info: ValidationInfo) -> bool:
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            normalized = v.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
            if info.field_name == "DEBUG" and normalized in {"release", "prod", "production"}:
                return False
            raise ValueError(
                f"Valor inválido para flag booleano '{info.field_name}': {v!r}. "
                "Valores aceptados: true, false, 1, 0, yes, no, on, off."
            )
        return bool(v)

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def parse_cors_origins(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            stripped = v.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                parsed = json.loads(stripped)
                if isinstance(parsed, list):
                    return [str(origin).strip() for origin in parsed if str(origin).strip()]
            return [origin.strip() for origin in stripped.split(",") if origin.strip()]
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
    USE_LOCAL_FALLBACK: bool = False
    # Timeouts/retries del cliente boto3 (R2). Acotados para que un cuelgue de red
    # falle ruidoso (FAILED) en vez de colgar la task hasta el SIGKILL de Celery.
    S3_CONNECT_TIMEOUT: int = 10
    S3_READ_TIMEOUT: int = 30
    S3_MAX_ATTEMPTS: int = 2

    # ── Email (Resend HTTP API) ────────────────────────────────────────────────
    RESEND_API_KEY: str = ""
    SMTP_PASSWORD: str = (
        ""  # alias legacy — se usa como RESEND_API_KEY si RESEND_API_KEY está vacío
    )
    SMTP_FROM_EMAIL: str = "noreply@vektor.app"
    # Destinatario de los leads del formulario público de contacto. OBLIGATORIO
    # en producción (ver validador abajo). En dev, si queda vacío, se usa
    # SMTP_FROM_EMAIL como fallback. Poné acá el mail oficial de Véktor.
    CONTACT_LEAD_EMAIL: str = ""

    # ── Comunicación: WhatsApp (DORMIDO — Fase posterior) ──────────────────────
    # Canal WhatsApp vía Meta Cloud API. Default OFF: requiere verificación de la
    # app en Meta + número aprobado. Mientras ENABLE_WHATSAPP=False el adapter
    # levanta ChannelNotConfigured y el router responde 503.
    ENABLE_WHATSAPP: bool = False
    WHATSAPP_TOKEN: str = ""
    WHATSAPP_PHONE_ID: str = ""

    # ── Marketing: sincronización de redes (DORMIDO — Fase posterior) ──────────
    # Sync automático de métricas de redes sociales vía OAuth (Meta/TikTok).
    # Default OFF: requiere verificación de la app (OAuth). Por ahora la carga de
    # métricas es 100% manual; los adapters levantan NotImplementedError.
    ENABLE_SOCIAL_SYNC: bool = False
    META_APP_ID: str = ""
    META_APP_SECRET: str = ""
    TIKTOK_CLIENT_KEY: str = ""
    TIKTOK_CLIENT_SECRET: str = ""

    # ── Feature flags ─────────────────────────────────────────────────────────
    ENABLE_SCORE_RECALCULATION: bool = True
    ENABLE_EMAIL_NOTIFICATIONS: bool = False
    ENABLE_EMAIL_VERIFICATION: bool = True
    ENABLE_AGENT_AUTOMATIONS: bool = False
    # FASE 2: 4ª capa de mapeo de columnas con LLM (fallback ante baja confianza).
    # Default False — opt-in: requiere ANTHROPIC_API_KEY y consume tokens.
    ENABLE_LLM_COLUMN_MAPPING: bool = False
    # FASE 2 (A1): detección de propósito del archivo por contenido con LLM.
    # Corre SOLO cuando la inferencia determinística queda "general" (ambigua).
    # Default False — opt-in: requiere ANTHROPIC_API_KEY y consume tokens.
    ENABLE_LLM_FILE_TYPE_DETECTION: bool = False
    # Modelo LLM para las capas de ingestión (mapeo de columnas + detección de tipo).
    # Configurable por env para no hardcodear el id del modelo en el código.
    LLM_INGESTION_MODEL: str = "claude-sonnet-4-6"
    SCORE_RECALC_COOLDOWN_SECONDS: int = 300
    # F4: TTL del lease per-file del confirm de ingestión. Un IMPORTING con
    # import_started_at más viejo que esto es candidato a takeover. CONSERVADOR y
    # por encima del máximo operativo esperado (un import grande sobre Neon puede
    # tardar minutos): un TTL corto arriesga interrumpir un import legítimo.
    INGESTION_IMPORT_LEASE_TTL_SECONDS: int = 900  # 15 min

    # Auth social
    ENABLE_GOOGLE_LOGIN: bool = False
    ENABLE_FACEBOOK_LOGIN: bool = False  # Diferido — solo abstracción en fase 1

    # ── Google MCP ────────────────────────────────────────────────────────────
    # False (default): agentes informan pero no ejecutan ops Google
    # True: el backend llama al MCP server en MCP_SERVER_URL
    ENABLE_GOOGLE_MCP_TOOLS: bool = False
    MCP_SERVER_URL: str = ""
    MCP_HTTP_TIMEOUT: float = 15.0
    MCP_SERVER_SHARED_SECRET: str = ""

    # ── Google OAuth ──────────────────────────────────────────────────────────
    GOOGLE_OAUTH_CLIENT_ID: str = ""
    GOOGLE_OAUTH_CLIENT_SECRET: str = ""
    # Redirect URI para login social (Google Cloud Console)
    GOOGLE_OAUTH_REDIRECT_URI: str = "http://localhost:8000/api/v1/auth/oauth/google/callback"

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

    # ── LLM Providers ─────────────────────────────────────────────────────────
    ANTHROPIC_API_KEY: str = ""
    # Timeout de la conexión HTTP al cliente Anthropic (segundos).
    # El SDK 0.49+ acepta este kwarg en el constructor.
    # max_retries=0 porque el retry transitorio lo maneja el executor (F0.4).
    ANTHROPIC_TIMEOUT_SECONDS: float = 12.0
    # Timeout máximo por task en el TeamPlanExecutor (F0.3).
    # Un agente colgado cancela la coroutine dentro de este plazo.
    AGENT_TASK_TIMEOUT_SECONDS: float = 20.0

    # ── Production secret validation ──────────────────────────────────────────
    @model_validator(mode="after")
    def validate_production_secrets(self) -> "Settings":
        # Always include FRONTEND_URL in CORS_ORIGINS so the app works even if
        # CORS_ORIGINS is not explicitly set to include the production domain.
        if self.FRONTEND_URL and self.FRONTEND_URL not in self.CORS_ORIGINS:
            self.CORS_ORIGINS = [self.FRONTEND_URL, *self.CORS_ORIGINS]

        if self.is_development:
            merged_origins = list(
                dict.fromkeys([*self.CORS_ORIGINS, *self.__class__.DEV_CORS_ORIGINS])
            )
            self.CORS_ORIGINS = merged_origins

        if self.DEBUG or self.DEMO_MODE:
            # In debug/demo mode, skip email verification so local testing isn't blocked.
            self.ENABLE_EMAIL_VERIFICATION = False

        if self.ENVIRONMENT == "production":
            insecure_defaults = {"insecure-change-me", "insecure-jwt-change-me", ""}
            if self.SECRET_KEY in insecure_defaults or len(self.SECRET_KEY) < 32:
                raise ValueError(
                    "SECRET_KEY must be at least 32 characters "
                    "and not a default value in production."
                )
            if self.JWT_SECRET_KEY in insecure_defaults or len(self.JWT_SECRET_KEY) < 32:
                raise ValueError(
                    "JWT_SECRET_KEY must be at least 32 characters "
                    "and not a default value in production."
                )

        # ── Fail-fast: credenciales OAuth requeridas cuando los flags están activos ──
        # Se valida en cualquier entorno (no solo producción) para detectar
        # configuraciones rotas antes del primer request OAuth.
        if self.ENABLE_GOOGLE_LOGIN:
            missing: list[str] = []
            if not self.GOOGLE_OAUTH_CLIENT_ID:
                missing.append("GOOGLE_OAUTH_CLIENT_ID")
            if not self.GOOGLE_OAUTH_CLIENT_SECRET:
                missing.append("GOOGLE_OAUTH_CLIENT_SECRET")
            if self.ENABLE_GOOGLE_LOGIN and not self.GOOGLE_OAUTH_REDIRECT_URI:
                missing.append("GOOGLE_OAUTH_REDIRECT_URI")
            if missing:
                flags = "ENABLE_GOOGLE_LOGIN"
                raise ValueError(
                    f"Los flags [{flags}] están activos pero faltan las credenciales: "
                    f"{', '.join(missing)}"
                )

        if self.ENABLE_GOOGLE_MCP_TOOLS and not self.MCP_SERVER_URL:
            raise ValueError("MCP_SERVER_URL es requerido cuando ENABLE_GOOGLE_MCP_TOOLS=true")

        if self.ENABLE_GOOGLE_MCP_TOOLS and not self.MCP_SERVER_SHARED_SECRET:
            import logging as _logging  # noqa: PLC0415

            _logging.getLogger(__name__).warning(
                "config.mcp.shared_secret_missing: "
                "ENABLE_GOOGLE_MCP_TOOLS=true pero MCP_SERVER_SHARED_SECRET está vacío. "
                "Las llamadas al MCP server no incluirán header de autenticación."
            )

        # ── Email: validar combo flag + credencial ────────────────────────────
        _has_email_key = bool(self.RESEND_API_KEY or self.SMTP_PASSWORD)
        if self.ENABLE_EMAIL_VERIFICATION and not _has_email_key:
            raise ValueError(
                "ENABLE_EMAIL_VERIFICATION=true requiere RESEND_API_KEY (o SMTP_PASSWORD) "
                "configurado."
            )
        if self.ENABLE_EMAIL_NOTIFICATIONS and not _has_email_key:
            raise ValueError("ENABLE_EMAIL_NOTIFICATIONS=true requiere RESEND_API_KEY configurado.")

        # ── WhatsApp: validar combo flag + credencial ──────────────────────────
        if self.ENABLE_WHATSAPP and not self.WHATSAPP_TOKEN:
            raise ValueError(
                "ENABLE_WHATSAPP=true requiere WHATSAPP_TOKEN (Meta Cloud API) configurado."
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
        """connect_args para asyncpg: SSL (Neon, RDS) + timeouts acotados.

        `timeout` limita el establecimiento de conexión y `command_timeout` cada
        query, para que un stall de Neon surja como error (y la task marque FAILED)
        en vez de colgarse hasta el SIGKILL de Celery.
        """
        args: dict = {  # type: ignore[type-arg]
            "timeout": self.PG_CONNECT_TIMEOUT,
            "command_timeout": self.PG_COMMAND_TIMEOUT,
        }
        if self.DATABASE_URL_RAW and "sslmode=require" in self.DATABASE_URL_RAW:
            import ssl  # noqa: PLC0415

            args["ssl"] = ssl.create_default_context()
        return args

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"

    @property
    def is_development(self) -> bool:
        return self.ENVIRONMENT == "development"

    @property
    def contact_lead_recipient(self) -> str:
        """Destinatario resuelto para los leads del formulario de contacto.

        CONTACT_LEAD_EMAIL si está configurado; en dev cae a SMTP_FROM_EMAIL.
        En producción sin CONTACT_LEAD_EMAIL devuelve "" a propósito: la tarea
        de email lo trata como fallo (loguea + marca failed) SIN perder el lead,
        en vez de mandar el aviso a una casilla no operativa.
        """
        if self.CONTACT_LEAD_EMAIL:
            return self.CONTACT_LEAD_EMAIL
        if self.is_development:
            return self.SMTP_FROM_EMAIL
        return ""

    @classmethod
    def settings_customise_sources(  # type: ignore[override]
        cls,
        settings_cls: type[BaseSettings],
        init_settings: EnvSettingsSource,
        env_settings: EnvSettingsSource,
        dotenv_settings: EnvSettingsSource,
        **kwargs: EnvSettingsSource,
    ) -> tuple[EnvSettingsSource, ...]:
        return (
            init_settings,
            _LenientEnvSource(settings_cls),
            dotenv_settings,
            *kwargs.values(),
        )


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton. Loaded once at startup."""
    return Settings()
