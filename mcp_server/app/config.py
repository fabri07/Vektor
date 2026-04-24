from __future__ import annotations

import base64
import hashlib
import re
from functools import lru_cache

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

    GOOGLE_OAUTH_CLIENT_ID: str = ""
    GOOGLE_OAUTH_CLIENT_SECRET: str = ""
    GOOGLE_OAUTH_REDIRECT_URI: str = "http://localhost:8080/auth/callback"

    MCP_SERVER_SHARED_SECRET: str = ""
    GOOGLE_OAUTH_TIMEOUT_SECONDS: float = 20.0

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
    def validate_required(self) -> "Settings":
        missing: list[str] = []
        if not self.DATABASE_URL_RAW:
            missing.append("DATABASE_URL")
        if not self.GOOGLE_OAUTH_CLIENT_ID:
            missing.append("GOOGLE_OAUTH_CLIENT_ID")
        if not self.GOOGLE_OAUTH_CLIENT_SECRET:
            missing.append("GOOGLE_OAUTH_CLIENT_SECRET")
        if not self.GOOGLE_OAUTH_REDIRECT_URI:
            missing.append("GOOGLE_OAUTH_REDIRECT_URI")
        if missing:
            raise ValueError(f"Faltan variables requeridas: {', '.join(missing)}")
        return self

    @property
    def DATABASE_URL(self) -> str:  # noqa: N802
        assert self.DATABASE_URL_RAW
        return _to_asyncpg_url(self.DATABASE_URL_RAW)

    @property
    def token_cipher_key(self) -> bytes:
        digest = hashlib.sha256(self.SECRET_KEY.encode()).digest()
        return base64.urlsafe_b64encode(digest)


@lru_cache
def get_settings() -> Settings:
    return Settings()
