"""M·2c: costea las ventas anteriores a M·2a, siempre como ESTIMACIÓN.

La regla vive en ``app/application/services/costeo_retroactivo_service.py``.
Este script sólo elige los tenants, imprime el resumen y confirma o revierte.

- **Sin ``--apply`` (default) es un DRY-RUN:** calcula todo dentro de una transacción
  que se REVIERTE. No escribe nada.
- **``--apply``:** escribe el snapshot de cada venta sin costear y deja un
  ``data_repair_runs`` ``M2C_COSTEO_RETROACTIVO`` por tenant, con un item por venta.
  Sólo toca ventas SIN snapshot, así que reejecutarlo no duplica nada.
- **``--revert <run_id>``:** devuelve a NULL las ventas de esa corrida cuyo snapshot
  sigue siendo el que escribió.

Nunca imprime la connection URL: sólo ``host`` y ``base``.

Usage (desde ``backend/``):
    DATABASE_URL='postgresql://...' .venv/bin/python scripts/costeo_retroactivo_ventas.py \
        --email x@y.com
    ... --tenant-id <uuid> [--apply]
    ... --all [--apply]
    ... --revert <run_id>
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import uuid
from urllib.parse import urlparse

_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPTS)
sys.path.insert(0, os.path.dirname(_SCRIPTS))

from _db import async_engine_config  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker  # noqa: E402

from app.application.services.costeo_retroactivo_service import (  # noqa: E402
    Resumen,
    costear_historico,
    revertir,
)
from app.persistence.db.engine_factory import crear_engine  # noqa: E402
from app.persistence.models.transaction import SaleEntry  # noqa: E402


def _imprimir(r: Resumen, aplicar: bool, nombre: str) -> None:
    print(f"\n── {nombre} ({str(r.tenant_id)[:8]}…)  ventas sin costear: {r.ventas}")
    if not r.ventas:
        return
    for origen, n in r.por_origen.most_common():
        print(f"   origen {origen:14} {n:6}")
    print(
        f"   habrían salido confirmadas en vivo: {r.habrian_sido_confirmadas}"
        " (quedan como estimación)"
    )
    if r.sin_costear_por_error:
        print(
            f"   ⚠ sin costear por error: {r.sin_costear_por_error}"
            " (buscar costeo_de_ventas_fallo)"
        )
    cobertura = r.cobertura_pct
    margen = r.margen_estimado_pct
    print(
        f"   ingreso ${r.ingreso_total:,.2f} · con costo ${r.ingreso_con_costo:,.2f}"
        f" ({'—' if cobertura is None else f'{cobertura}%'}) · costo ${r.costo_total:,.2f}"
        f" · margen estimado sobre lo cubierto: {'—' if margen is None else f'{margen}%'}"
    )
    if aplicar and r.run_id:
        print(f"   APLICADO · run_id {r.run_id} (para revertir: --revert {r.run_id})")


async def _tenants(session: AsyncSession, args: argparse.Namespace) -> list[uuid.UUID]:
    if args.tenant_id:
        return [uuid.UUID(args.tenant_id)]
    if args.email:
        tid = (
            await session.execute(
                text("SELECT tenant_id FROM users WHERE lower(email) = lower(:e)"),
                {"e": args.email},
            )
        ).scalars().all()
        if len(tid) != 1:
            print(f"ERROR: el email corresponde a {len(tid)} tenants; usá --tenant-id.")
            sys.exit(2)
        return [tid[0]]
    filas = await session.execute(
        select(SaleEntry.tenant_id)
        .where(
            SaleEntry.product_id.is_not(None),
            SaleEntry.voided_at.is_(None),
            SaleEntry.cost_snapshot_version.is_(None),
        )
        .distinct()
    )
    return list(filas.scalars())


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    quien = ap.add_mutually_exclusive_group(required=True)
    quien.add_argument("--tenant-id")
    quien.add_argument("--email")
    quien.add_argument("--all", action="store_true")
    quien.add_argument("--revert", metavar="RUN_ID")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    # La configuración de logging de la app puede dejar el eco del SQL en INFO.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    url, connect_args = async_engine_config()
    p = urlparse(url)
    print(f"Base: host={p.hostname} base={(p.path or '/')[1:]}")
    print("MODO:", "REVERT" if args.revert else ("APPLY" if args.apply else "DRY-RUN (no escribe)"))
    engine = crear_engine(url, connect_args=connect_args, echo=False)
    fabrica = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with fabrica() as session:
            if args.revert:
                revertidas, salteadas = await revertir(session, uuid.UUID(args.revert))
                await session.commit()
                print(f"Revertidas: {revertidas} · salteadas (cambiaron después): {salteadas}")
                return
            for tenant_id in await _tenants(session, args):
                nombre = (
                    await session.execute(
                        text("SELECT display_name FROM tenants WHERE tenant_id = :t"),
                        {"t": tenant_id},
                    )
                ).scalar_one_or_none() or "(sin nombre)"
                resumen = await costear_historico(session, tenant_id, aplicar=args.apply)
                if args.apply:
                    await session.commit()
                else:
                    await session.rollback()
                _imprimir(resumen, args.apply, nombre)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
