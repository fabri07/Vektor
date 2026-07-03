"""Lista uploaded_files y pipeline_events en una ventana de tiempo, para detectar
si un mismo archivo se procesó (o reprocesó) dos veces.

Usage:
    DATABASE_URL='...' .venv/bin/python scripts/diag_uploads_window.py \
        --tenant <uuid> --from "2026-06-19 18:00:00" --to "2026-06-19 19:10:00"

ONLY runs SELECT statements. No writes. Safe against production.
"""

import argparse
import asyncio
import uuid
from datetime import datetime

from _db import async_engine_config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--from", dest="from_ts", required=True)
    parser.add_argument("--to", dest="to_ts", required=True)
    args = parser.parse_args()

    tid = uuid.UUID(args.tenant)
    from_ts = datetime.fromisoformat(args.from_ts)
    to_ts = datetime.fromisoformat(args.to_ts)

    url, connect_args = async_engine_config()
    engine = create_async_engine(url, connect_args=connect_args)
    async with AsyncSession(engine) as session:
        files = (
            await session.execute(
                text(
                    "SELECT id, original_filename, purpose, status, processing_status, "
                    "       content_hash, trace_id, created_at "
                    "FROM uploaded_files "
                    "WHERE tenant_id = :tid AND created_at BETWEEN :fts AND :tts "
                    "ORDER BY created_at"
                ),
                {"tid": tid, "fts": from_ts, "tts": to_ts},
            )
        ).mappings().all()
        print(f"{len(files)} uploaded_files en la ventana:\n")
        for f in files:
            print(dict(f))
            print()

        events = (
            await session.execute(
                text(
                    "SELECT stage, file_id, trace_id, created_at, detail "
                    "FROM pipeline_events "
                    "WHERE tenant_id = :tid AND created_at BETWEEN :fts AND :tts "
                    "ORDER BY created_at"
                ),
                {"tid": tid, "fts": from_ts, "tts": to_ts},
            )
        ).mappings().all()
        print(f"\n{len(events)} pipeline_events en la ventana:\n")
        for e in events:
            print(dict(e))
            print()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
