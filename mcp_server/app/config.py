from __future__ import annotations

import base64
import hashlib
import re
from functools import lru_cache
from urllib.parse import urlparse

from pydantic import Field, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _to_asyncpg_url(raw: str) -> str:
    url = raw.replace("postgresql://", "postgresql+asyncpg://", 1)
    url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    for param in ("channel_binding", "sslmode"):
        url = re.sub(rf"[?&]{param}=[^&]*", "", url)
    url = re.sub(r"\?$", "", url)
    url = re.sub(r"\?&", "?", url)
    return url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
        env_ignore_empty=True,
    )

    ENVIRONMENT: str = "development"
    DEBUG: bool = False
    SECRET_KEY: str = "insecure-change-me"

    DATABASE_URL_RAW: str | None = Field(default=None, alias="DATABASE_URL")

    # Prefer these MCP-specific variables in production. The plain
    # GOOGLE_OAUTH_* names remain as a backwards-compatible fallback, but they
    # are also used by the main backend for social login and can easily point to
    # the wrong callback.
    GOOGLE_MCP_OAUTH_CLIENT_ID: str = ""
    GOOGLE_MCP_OAUTH_CLIENT_SECRET: str = ""
    GOOGLE_MCP_OAUTH_REDIRECT_URI: str = ""

    GOOGLE_OAUTH_CLIENT_ID: str = ""
    GOOGLE_OAUTH_CLIENT_SECRET: str = ""
    GOOGLE_OAUTH_REDIRECT_URI: str = "http://localhost:8080/auth/callback"

    MCP_SERVER_SHARED_SECRET: str = ""
    GOOGLE_OAUTH_TIMEOUT_SECONDS: float = 20.0
    # Orígenes permitidos para CORS. En producción setear al dominio del backend.
    # Lista vacía = ningún origen de browser permitido (correcto para llamadas server-to-server).
    CORS_ALLOWED_ORIGINS: list[str] = []

    @field_validator("DEBUG", mode="before")
    @classmethod
    def parse_bool_flags(cls, value: bool | str, info: ValidationInfo) -> bool | str:
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
            if info.field_name == "DEBUG" and normalized in {"release", "prod", "production"}:
                return False
        return value

    @model_validator(mode="after")
    def validate_required(self) -> Settings:
        missing: list[str] = []
        if not self.DATABASE_URL_RAW:
            missing.append("DATABASE_URL")
        if not self.google_oauth_client_id:
            missing.append("GOOGLE_MCP_OAUTH_CLIENT_ID or GOOGLE_OAUTH_CLIENT_ID")
        if not self.google_oauth_client_secret:
            missing.append("GOOGLE_MCP_OAUTH_CLIENT_SECRET or GOOGLE_OAUTH_CLIENT_SECRET")
        if not self.google_oauth_redirect_uri:
            missing.append("GOOGLE_MCP_OAUTH_REDIRECT_URI or GOOGLE_OAUTH_REDIRECT_URI")
        if missing:
            raise ValueError(f"Faltan variables requeridas: {', '.join(missing)}")

        parsed_redirect = urlparse(self.google_oauth_redirect_uri)
        if parsed_redirect.scheme not in {"http", "https"} or not parsed_redirect.netloc:
            raise ValueError(
                "GOOGLE_MCP_OAUTH_REDIRECT_URI debe ser una URL absoluta http(s)"
            )
        if "/api/v1/auth" in parsed_redirect.path:
            raise ValueError(
                "GOOGLE_MCP_OAUTH_REDIRECT_URI apunta al callback de login social del backend. "
                "Debe apuntar al MCP server, normalmente https://<mcp-host>/auth/callback"
            )
        return self

    @property
    def DATABASE_URL(self) -> str:  # noqa: N802
        assert self.DATABASE_URL_RAW
        return _to_asyncpg_url(self.DATABASE_URL_RAW)

    @property
    def google_oauth_client_id(self) -> str:
        return self.GOOGLE_MCP_OAUTH_CLIENT_ID or self.GOOGLE_OAUTH_CLIENT_ID

    @property
    def google_oauth_client_secret(self) -> str:
        return self.GOOGLE_MCP_OAUTH_CLIENT_SECRET or self.GOOGLE_OAUTH_CLIENT_SECRET

    @property
    def google_oauth_redirect_uri(self) -> str:
        return self.GOOGLE_MCP_OAUTH_REDIRECT_URI or self.GOOGLE_OAUTH_REDIRECT_URI

    @property
    def token_cipher_key(self) -> bytes:
        digest = hashlib.sha256(self.SECRET_KEY.encode()).digest()
        return base64.urlsafe_b64encode(digest)


@lru_cache
def get_settings() -> Settings:
    return Settings()
