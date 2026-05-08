#!/usr/bin/env python3
"""
Seed vertical field definitions from JSON files.

Idempotent upsert: safe to run multiple times.
New JSON files in app/application/data/vertical_fields/ are picked up automatically.

Usage:
    make seed-vertical-fields
"""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://vektor:vektor@localhost:5432/vektor",
)

DATA_DIR = Path(__file__).parent.parent / "app" / "application" / "data" / "vertical_fields"

VALID_DATA_TYPES = {"text", "number", "date", "enum", "boolean"}
VALID_ENTITY_TYPES = {"sale", "expense", "product", "inventory"}


def _async_database_url(raw_url: str) -> tuple[str, dict]:
    """Convert platform Postgres URLs into SQLAlchemy asyncpg URLs.

    Neon/Railway often expose sync-looking DATABASE_URL values. This seed script uses
    SQLAlchemy async, so normalize the driver and move SSL requirements to
    asyncpg-compatible connect_args.
    """
    parsed = urlsplit(raw_url)
    scheme = parsed.scheme
    if scheme in {"postgresql", "postgres"}:
        scheme = "postgresql+asyncpg"
    elif scheme in {"postgresql+psycopg2", "postgresql+psycopg"}:
        scheme = "postgresql+asyncpg"

    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    ssl_required = any(k == "sslmode" and v == "require" for k, v in query_pairs)
    filtered_query = urlencode(
        [(k, v) for k, v in query_pairs if k not in {"sslmode", "channel_binding"}]
    )
    normalized = urlunsplit(
        (scheme, parsed.netloc, parsed.path, filtered_query, parsed.fragment)
    )
    connect_args = {}
    if ssl_required:
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        connect_args["ssl"] = ssl_context
    return normalized, connect_args


def _validate_entry(entry: dict, source_file: str) -> None:
    required = {"entity_type", "field_key", "label", "data_type"}
    missing = required - entry.keys()
    if missing:
        raise ValueError(f"{source_file}: entry {entry.get('field_key')!r} missing {missing}")
    if entry["data_type"] not in VALID_DATA_TYPES:
        raise ValueError(f"{source_file}: invalid data_type {entry['data_type']!r}")
    if entry["entity_type"] not in VALID_ENTITY_TYPES:
        raise ValueError(f"{source_file}: invalid entity_type {entry['entity_type']!r}")
    if entry["data_type"] == "enum" and not entry.get("enum_options"):
        raise ValueError(f"{source_file}: enum field {entry['field_key']!r} must have enum_options")


async def seed_vertical(session: AsyncSession, vertical_code: str, entries: list[dict]) -> int:
    upserted = 0
    now = datetime.now(UTC)
    for entry in entries:
        existing = await session.execute(
            text(
                "SELECT id FROM vertical_field_definitions "
                "WHERE vertical_code = :vc AND field_key = :fk AND entity_type = :et"
            ),
            {"vc": vertical_code, "fk": entry["field_key"], "et": entry["entity_type"]},
        )
        row = existing.fetchone()
        if row:
            await session.execute(
                text(
                    "UPDATE vertical_field_definitions SET "
                    "label = :label, data_type = :dt, enum_options = CAST(:eo AS jsonb), "
                    "is_required = :ir, display_order = :do_, context_weight = :cw "
                    "WHERE vertical_code = :vc AND field_key = :fk AND entity_type = :et"
                ),
                {
                    "label": entry["label"],
                    "dt": entry["data_type"],
                    "eo": json.dumps(entry.get("enum_options")) if entry.get("enum_options") else None,
                    "ir": entry.get("is_required", False),
                    "do_": entry.get("display_order", 0),
                    "cw": entry.get("context_weight", 0.0),
                    "vc": vertical_code,
                    "fk": entry["field_key"],
                    "et": entry["entity_type"],
                },
            )
        else:
            await session.execute(
                text(
                    "INSERT INTO vertical_field_definitions "
                    "(vertical_code, entity_type, field_key, label, data_type, enum_options, "
                    "is_required, display_order, context_weight, affects_scoring, created_at) "
                    "VALUES (:vc, :et, :fk, :label, :dt, CAST(:eo AS jsonb), :ir, :do_, :cw, false, :now)"
                ),
                {
                    "vc": vertical_code,
                    "et": entry["entity_type"],
                    "fk": entry["field_key"],
                    "label": entry["label"],
                    "dt": entry["data_type"],
                    "eo": json.dumps(entry.get("enum_options")) if entry.get("enum_options") else None,
                    "ir": entry.get("is_required", False),
                    "do_": entry.get("display_order", 0),
                    "cw": entry.get("context_weight", 0.0),
                    "now": now,
                },
            )
            upserted += 1
    await session.commit()
    return upserted


async def _ensure_schema_ready(session: AsyncSession) -> None:
    result = await session.execute(text("SELECT to_regclass('public.vertical_field_definitions')"))
    if result.scalar_one() is None:
        raise RuntimeError(
            "La tabla vertical_field_definitions no existe. "
            "Ejecutá primero las migraciones con la misma DATABASE_URL: make migrate"
        )


async def main() -> None:
    json_files = sorted(DATA_DIR.glob("*.json"))
    if not json_files:
        print(f"No JSON files found in {DATA_DIR}")
        return

    database_url, connect_args = _async_database_url(DATABASE_URL)
    engine = create_async_engine(database_url, pool_pre_ping=True, connect_args=connect_args)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as session:
        await _ensure_schema_ready(session)
        total_new = 0
        for path in json_files:
            vertical_code = path.stem
            with open(path, encoding="utf-8") as f:
                entries = json.load(f)

            for entry in entries:
                _validate_entry(entry, path.name)

            new_count = await seed_vertical(session, vertical_code, entries)
            status = f"{new_count} nuevos" if new_count else "sin cambios (ya existían)"
            print(f"  {vertical_code}: {len(entries)} campos — {status}")
            total_new += new_count

    await engine.dispose()
    print(f"\nTotal: {total_new} campos insertados.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
