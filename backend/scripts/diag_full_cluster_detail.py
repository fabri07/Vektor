"""Detalle read-only de TODOS los clusters de B1 (repair_inventory_ledger.py) con su
razón de clasificación — para revisión humana antes de --apply cuando el heurístico
promueve muchos más clusters de lo esperado (ej. BATCH_TIMING con timing compartido
entre movement_types distintos).

Usage:
    DATABASE_URL='...' .venv/bin/python scripts/diag_full_cluster_detail.py \
        --tenant <uuid> --out full_detail.csv

ONLY runs SELECT statements (vía _plan_b1_dedup, que es read-only). No writes.
"""

import argparse
import asyncio
import csv
import uuid

from _db import async_engine_config
from repair_inventory_ledger import _plan_b1_dedup
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--out", default="full_cluster_detail.csv")
    args = parser.parse_args()

    tid = uuid.UUID(args.tenant)
    url, connect_args = async_engine_config()
    engine = create_async_engine(url, connect_args=connect_args)
    async with AsyncSession(engine) as session:
        b1 = await _plan_b1_dedup(session, tid)
        clusters = sorted(
            b1["clusters"], key=lambda c: (c["reason"], c["movement_type"], c["day"])
        )
        with open(args.out, "w", newline="", encoding="utf-8") as fh:
            fieldnames = sorted({k for c in clusters for k in c})
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(clusters)
        print(f"{len(clusters)} cluster(s) escritos en {args.out}.")
        print(f"by_conf: {dict(b1['by_conf'])}")
        print(f"by_reason: {dict(b1['by_reason'])}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
