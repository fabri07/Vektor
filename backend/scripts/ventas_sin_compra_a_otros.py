"""B·5g: las ventas históricas sin producto o sin compra previa pasan a «Otros».

La regla vive en ``app/application/services/reparacion_ventas_sin_compra_service.py``.
Desde B·5e ninguna venta NUEVA entra sin producto o sin compra/stock inicial con
costo a su fecha; esto trata las que ya estaban, con la autorización del dueño.

- **Default: ANÁLISIS, sólo lectura** (``SELECT``, sin locks). Es lo que corre
  ``--all``: cuántas, cuánto, por mes y por motivo. Las del POS no se tocan.
- **``--apply``** (un negocio, ``--email`` o ``--tenant-id``): en una transacción
  y con el lease de mantenimiento (frena las escrituras del local: FUERA DE
  HORARIO), anula cada una, devuelve su descuento de stock y deja su fila en
  «Otros» con el motivo. Bajan los ingresos de esos días hasta que se resuelvan.
- **``--desde``/``--hasta``** (AAAA-MM-DD) limitan por fecha de la venta.
- **``--revert <run_id>``:** restaura las que siguen PENDING en «Otros»; las que
  el usuario ya resolvió o descartó quedan como están.

Nunca imprime la connection URL: sólo ``host`` y ``base``. Después de ``--apply``
o ``--revert``, correr ``scripts/refresh_health_score.py`` con el email.

Usage (desde ``backend/``):
    DATABASE_URL='postgresql://...' .venv/bin/python scripts/ventas_sin_compra_a_otros.py --all
    ... --email x@y.com [--desde 2026-01-01] [--hasta 2026-09-30] [--apply]
    ... --revert <run_id>
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import uuid
from datetime import date
from urllib.parse import urlparse

_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPTS)
sys.path.insert(0, os.path.dirname(_SCRIPTS))

from _db import async_engine_config  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker  # noqa: E402

from app.application.services import maintenance_lock_service as mls  # noqa: E402
from app.application.services.reparacion_ventas_sin_compra_service import (  # noqa: E402
    REPAIR_TYPE,
    Resumen,
    analizar,
    reparar,
    revertir,
)
from app.persistence.db.engine_factory import crear_engine  # noqa: E402
from app.persistence.models.repair import DataRepairRun  # noqa: E402
from app.persistence.models.transaction import SaleEntry  # noqa: E402

LEASE_TTL = 900
CREADO_POR = "script:ventas_sin_compra_a_otros"


def imprimir(r: Resumen, modo: str, nombre: str) -> None:
    print(f"\n── {nombre} ({str(r.tenant_id)[:8]}…) · {modo}")
    print(f"   ventas sin compra previa o sin producto: {len(r.candidatas)} · ${r.monto:,.2f}")
    for motivo, n in r.por_motivo().most_common():
        print(f"   {motivo}: {n}")
    for mes, monto in r.por_mes().items():
        print(f"   {mes}: los ingresos de ese mes bajan ${monto:,.2f}")
    if modo == "APPLY" and r.run_id:
        print(f"   APLICADO · run_id {r.run_id} (para revertir: --revert {r.run_id})")
        print("   El score NO se recalcula solo: correr scripts/refresh_health_score.py.")


def _fecha(v: str | None) -> date | None:
    return None if v is None else date.fromisoformat(v)


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
        select(SaleEntry.tenant_id).where(SaleEntry.voided_at.is_(None)).distinct()
    )
    return list(filas.scalars())


async def _con_lease(session: AsyncSession, tenant_id: uuid.UUID, motivo: str) -> uuid.UUID | None:
    lease_id = await mls.acquire(
        session, tenant_id, reason=motivo, ttl_seconds=LEASE_TTL, created_by=CREADO_POR
    )
    await session.commit()
    if lease_id is None:
        print(f"⚠ tenant {tenant_id}: ya hay un lease de mantenimiento activo. SALTEADO.")
    return lease_id


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    quien = ap.add_mutually_exclusive_group(required=True)
    quien.add_argument("--tenant-id")
    quien.add_argument("--email")
    quien.add_argument("--all", action="store_true")
    quien.add_argument("--revert", metavar="RUN_ID")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--desde")
    ap.add_argument("--hasta")
    return ap


def validar(args: argparse.Namespace) -> str | None:
    if args.apply and (args.all or args.revert):
        return "ERROR: --apply va con --email o --tenant-id (un negocio por vez)."
    if args.revert and (args.desde or args.hasta):
        return "ERROR: --revert deshace la corrida entera: no lleva --desde/--hasta."
    return None


async def _revertir(session: AsyncSession, run_id: uuid.UUID) -> None:
    run = await session.get(DataRepairRun, run_id)
    if run is None or run.repair_type != REPAIR_TYPE or run.tenant_id is None:
        print(f"ERROR: {run_id} no es una corrida de {REPAIR_TYPE}.")
        sys.exit(2)
    lease = await _con_lease(session, run.tenant_id, "b5g_revert")
    if lease is None:
        return
    try:
        res = await revertir(session, run_id)
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    finally:
        await mls.release(session, lease)
        await session.commit()
    print(
        f"Restauradas: {len(res.restauradas)} · ya resueltas por el usuario (no se tocan): "
        f"{len(res.resueltas_por_el_usuario)}"
    )


async def main() -> None:
    args = parser().parse_args()
    error = validar(args)
    if error:
        print(error)
        sys.exit(2)
    desde, hasta = _fecha(args.desde), _fecha(args.hasta)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    url, connect_args = async_engine_config()
    p = urlparse(url)
    print(f"Base: host={p.hostname} base={(p.path or '/')[1:]}")
    modo = "REVERT" if args.revert else "APPLY" if args.apply else "ANÁLISIS (sólo lectura)"
    print("MODO:", modo)
    engine = crear_engine(url, connect_args=connect_args, echo=False)
    fabrica = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with fabrica() as session:
            if args.revert:
                await _revertir(session, uuid.UUID(args.revert))
                return
            for tenant_id in await _tenants(session, args):
                nombre = (
                    await session.execute(
                        text("SELECT display_name FROM tenants WHERE tenant_id = :t"),
                        {"t": tenant_id},
                    )
                ).scalar_one_or_none() or "(sin nombre)"
                if not args.apply:
                    resumen = await analizar(session, tenant_id, desde, hasta)
                    await session.rollback()
                    if resumen.candidatas or not args.all:
                        imprimir(resumen, modo, nombre)
                    continue
                lease = await _con_lease(session, tenant_id, "b5g_ventas_sin_compra")
                if lease is None:
                    continue
                try:
                    resumen = await reparar(
                        session, tenant_id, desde=desde, hasta=hasta, creado_por=CREADO_POR
                    )
                    await session.commit()
                except BaseException:
                    await session.rollback()
                    raise
                finally:
                    await mls.release(session, lease)
                    await session.commit()
                imprimir(resumen, modo, nombre)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
