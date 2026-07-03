"""Lista decision_audit_log en una ventana de tiempo puntual, para identificar qué
job/servicio generó un batch de movimientos sospechoso.

Usage:
    DATABASE_URL='...' .venv/bin/python scripts/diag_audit_log_window.py \
        --tenant <uuid> --from "2026-06-19 18:20:00" --to "2026-06-19 19:00:00"

ONLY runs SELECT statements. No writes. Safe against production.
"""

import argparse
import asyncio
import collections
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
        rows = (
            await session.execute(
                text(
                    "SELECT decision_type, created_at, decision_data "
                    "FROM decision_audit_log "
                    "WHERE tenant_id = :tid AND created_at BETWEEN :fts AND :tts "
                    "ORDER BY created_at"
                ),
                {"tid": tid, "fts": from_ts, "tts": to_ts},
            )
        ).mappings().all()

        print(f"{len(rows)} fila(s) de audit log en la ventana.\n")
        by_type: collections.Counter[str] = collections.Counter()
        for r in rows:
            by_type[r["decision_type"]] += 1
        print("Por decision_type:", dict(by_type))
        print()
        seen_types: set[str] = set()
        for r in rows:
            if r["decision_type"] not in seen_types:
                seen_types.add(r["decision_type"])
                print(f"--- primer ejemplo de {r['decision_type']} ({r['created_at']}) ---")
                print(r["decision_data"])
                print()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
