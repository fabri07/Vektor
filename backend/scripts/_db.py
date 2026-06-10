"""Helpers de conexión compartidos por los scripts operativos.

Una sola implementación de la normalización de DATABASE_URL (Neon agrega
``sslmode=require&channel_binding=require`` y asyncpg rechaza ambos como
kwargs de DSN). NUNCA imprime la URL.
"""

from __future__ import annotations

import os
import ssl
import sys
from typing import Any
from urllib.parse import parse_qs, urlparse, urlunparse


def normalize_dsn(raw: str) -> tuple[str, ssl.SSLContext | bool]:
    """Limpia sufijos de driver y extrae sslmode para asyncpg (vía urlparse)."""
    dsn = raw.strip()
    for suffix in ("+asyncpg", "+psycopg2", "+psycopg"):
        dsn = dsn.replace(suffix, "")
    parsed = urlparse(dsn)
    params = parse_qs(parsed.query)
    sslmode = (params.pop("sslmode", ["require"]) or ["require"])[0]
    # rebuild query sin sslmode/channel_binding (asyncpg rechaza kwargs desconocidos)
    params.pop("channel_binding", None)
    new_query = "&".join(f"{k}={v[0]}" for k, v in params.items())
    clean = urlunparse(parsed._replace(query=new_query))
    if sslmode in ("disable", "allow"):
        return clean, False
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return clean, ctx


def async_engine_config() -> tuple[str, dict[str, Any]]:
    """(url, connect_args) para create_async_engine desde DATABASE_URL del env."""
    raw = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_SYNC")
    if not raw:
        print("ERROR: exportá DATABASE_URL antes de correr.")
        sys.exit(2)
    clean, sslarg = normalize_dsn(raw)
    url = clean.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url, {"ssl": sslarg}
