"""Cómo se resuelve la base contra la que corren las migraciones.

Vive acá y no adentro de ``migrations/env.py`` porque hay dos consumidores que
DEBEN coincidir: el propio ``env.py`` y el preflight que ``scripts/migrate.sh``
imprime antes de migrar. Si cada uno resolviera la URL por su cuenta, el
preflight podría reportar sobre una base distinta de la que alembic termina
tocando — que es justo el modo de falla que el preflight existe para detectar.

``migrations/`` no es un paquete importable (no tiene ``__init__.py`` y alembic
lo carga por path), así que el módulo compartido tiene que estar afuera.
"""

from __future__ import annotations

import re
from typing import NamedTuple
from urllib.parse import urlsplit


class SyncUrl(NamedTuple):
    """URL sync resuelta + de dónde salió (para poder decirlo en el log)."""

    url: str
    source: str


def resolve_sync_url(ini_fallback: str | None = None, env: dict[str, str] | None = None) -> SyncUrl:
    """Resuelve la URL sync: ``DATABASE_URL_SYNC`` > ``DATABASE_URL`` > alembic.ini.

    ``env`` se inyecta solo en tests; en producción sale de ``os.environ``.
    """
    if env is None:
        import os  # noqa: PLC0415

        env = dict(os.environ)

    if url := env.get("DATABASE_URL_SYNC"):
        return SyncUrl(url, "DATABASE_URL_SYNC")
    if raw := env.get("DATABASE_URL"):
        # Convert postgresql:// → postgresql+psycopg2://, strip channel_binding
        url = raw.replace("postgresql://", "postgresql+psycopg2://", 1)
        url = url.replace("postgres://", "postgresql+psycopg2://", 1)
        url = re.sub(r"[?&]channel_binding=[^&]*", "", url)
        url = re.sub(r"\?$", "", url)
        url = re.sub(r"\?&", "?", url)
        return SyncUrl(url, "DATABASE_URL")
    return SyncUrl(ini_fallback or "", "alembic.ini")


def describe_target(url: str) -> str:
    """``host:puerto/base`` — lo que se puede loguear sin filtrar credenciales.

    Deliberadamente NO devuelve la URL: usuario y contraseña nunca van al log.
    El host sí, porque es exactamente el dato que permite ver que un deploy está
    migrando contra una base que no es la que uno cree.
    """
    parts = urlsplit(url)
    host = parts.hostname or "(sin host)"
    port = f":{parts.port}" if parts.port else ""
    database = parts.path.lstrip("/") or "(sin base)"
    return f"{host}{port}/{database}"
