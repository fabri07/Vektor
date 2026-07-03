"""Detalle read-only de un cluster puntual de inventory_movements (member-level).

Para inspeccionar los 2 movimientos de un cluster LOW específico: timestamps exactos,
source_row_hash, source_upload_id, y el registro de decision_audit_log más cercano en
el tiempo (por si algún job/script los generó).

Usage:
    DATABASE_URL='...' .venv/bin/python scripts/diag_adjustment_outliers.py \
        --tenant <uuid> --product "Agua Villavicencio 1.5L" --day 2026-06-19 \
        --movement-type adjustment --qty -258

ONLY runs SELECT statements. No writes. Safe against production.
"""

import argparse
import asyncio
import uuid
from datetime import date

from _db import async_engine_config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--product", required=True, help="Nombre exacto del producto")
    parser.add_argument(
        "--day", required=True, type=date.fromisoformat, help="YYYY-MM-DD"
    )
    parser.add_argument("--movement-type", required=True)
    parser.add_argument("--qty", required=True, type=int)
    args = parser.parse_args()

    tid = uuid.UUID(args.tenant)
    url, connect_args = async_engine_config()
    engine = create_async_engine(url, connect_args=connect_args)
    async with AsyncSession(engine) as session:
        pid_row = (
            await session.execute(
                text(
                    "SELECT id FROM products WHERE tenant_id = :tid AND name = :name"
                ),
                {"tid": tid, "name": args.product},
            )
        ).first()
        if pid_row is None:
            print("Producto no encontrado con ese nombre exacto.")
            return
        pid = pid_row[0]

        members = (
            await session.execute(
                text(
                    "SELECT id, qty, unit_cost, source_type, source_upload_id, "
                    "       source_row_ref, source_row_hash, supplier_id, created_at, "
                    "       voided_at "
                    "FROM inventory_movements "
                    "WHERE tenant_id = :tid AND product_id = :pid "
                    "AND movement_type = :mt AND date(created_at) = :d AND qty = :qty "
                    "ORDER BY created_at"
                ),
                {"tid": tid, "pid": pid, "mt": args.movement_type, "d": args.day, "qty": args.qty},
            )
        ).mappings().all()

        print(f"{len(members)} movimiento(s) encontrados:\n")
        for m in members:
            print(dict(m))
            print()

        if len(members) >= 2:
            ts = [m["created_at"] for m in members]
            delta = (ts[-1] - ts[0]).total_seconds()
            print(f"Delta entre el primero y el último: {delta:.3f}s")

        # Uploads relacionados por trace, si source_upload_id está seteado en alguno
        upload_ids = {m["source_upload_id"] for m in members if m["source_upload_id"]}
        for uid in upload_ids:
            up = (
                await session.execute(
                    text(
                        "SELECT original_filename, created_at, processing_status "
                        "FROM uploaded_files WHERE id = :uid"
                    ),
                    {"uid": uid},
                )
            ).first()
            print(f"\nupload {uid}: {up}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
