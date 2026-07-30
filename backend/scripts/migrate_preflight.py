#!/usr/bin/env python3
"""Deja asentado en el log del deploy CONTRA QUÉ BASE se va a migrar.

Un deploy que migra tiene que decir qué base tocó. Sin esto, cuando el esquema y
el `alembic_version` no coinciden con lo que uno ve desde su shell, no hay forma
de distinguir "otra base", "otro schema" o "log viejo" sin varias rondas de ida y
vuelta — que es exactamente lo que pasó en el deploy de `20260808_0001`.

Imprime, resolviendo la URL con el MISMO código que `migrations/env.py`
(`app.persistence.db.alembic_url`), para que lo reportado sea sí o sí lo migrado:

* de qué variable de entorno salió la URL (`DATABASE_URL_SYNC` le gana a
  `DATABASE_URL`, y esa precedencia es fácil de olvidar),
* host, puerto y nombre de base — NUNCA usuario ni contraseña,
* `current_database`/`current_user`/`current_schema`/`search_path`, que es lo que
  decide a qué tabla resuelve un nombre sin calificar,
* TODAS las tablas `alembic_version` visibles con sus filas: si hay más de una,
  alembic puede estar leyendo la versión de un schema y escribiendo el DDL en
  otro.

Solo SELECT. Y **nunca corta el deploy**: ante cualquier error sale con 0 y deja
que la autoridad siga siendo `alembic upgrade head`. Un paso de diagnóstico que
puede voltear un deploy es un pasivo, no un activo.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import create_engine, text  # noqa: E402

from app.persistence.db.alembic_url import describe_target, resolve_sync_url  # noqa: E402


def _p(msg: str) -> None:
    print(f"[preflight] {msg}", flush=True)


def main() -> None:
    resolved = resolve_sync_url()
    if not resolved.url:
        _p("no hay URL de base resuelta (ni DATABASE_URL_SYNC ni DATABASE_URL)")
        return

    _p(f"URL tomada de : {resolved.source}")
    _p(f"destino       : {describe_target(resolved.url)}")

    engine = create_engine(resolved.url, pool_pre_ping=False)
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT current_database() AS db, current_user AS usr, "
                "current_schema() AS sch, current_setting('search_path') AS path"
            )
        ).one()
        _p(f"database      : {row.db}")
        _p(f"user          : {row.usr}")
        _p(f"current_schema: {row.sch}")
        _p(f"search_path   : {row.path}")

        schemas = [
            r[0]
            for r in conn.execute(
                text(
                    "SELECT table_schema FROM information_schema.tables "
                    "WHERE table_name = 'alembic_version' ORDER BY table_schema"
                )
            )
        ]
        if not schemas:
            _p("alembic_version: no existe todavía (base nueva)")
        for schema in schemas:
            versions = [
                r[0]
                for r in conn.execute(text(f'SELECT version_num FROM "{schema}".alembic_version'))
            ]
            _p(f"alembic_version: {schema} = {versions}")
        if len(schemas) > 1:
            _p("AVISO: hay más de una alembic_version — el search_path decide cuál se lee")
    engine.dispose()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - diagnóstico: nunca voltea el deploy
        _p(f"no se pudo diagnosticar ({type(exc).__name__}: {exc}); sigue el deploy igual")
    sys.exit(0)
