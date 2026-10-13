"""M·R: anula los gastos «Stock inicial» de un catálogo importado como COMPRA.

La regla vive en ``app/application/services/reparacion_stock_inicial_service.py``.
Este script elige el tenant, toma el lease de mantenimiento, imprime el resumen y
confirma o revierte.

- **Sin ``--apply`` (default) es un DRY-RUN:** hace todo dentro de una transacción
  que se REVIERTE, para medir el efecto real. No escribe nada ni toma el lease.
- **``--apply``:** anula los gastos con par exacto (archivo + fila + producto +
  importe), vuelve a «apertura» la elección guardada de esos archivos y deja un
  ``data_repair_runs`` ``MR_STOCK_INICIAL_APERTURA``. El movimiento de inventario,
  el stock y el costo NO se tocan. Sólo un tenant por vez (``--email`` o
  ``--tenant-id``): «La compré ahora» puede ser cierto en otro negocio.
- **``--file-id``** (repetible) limita los GASTOS a esos archivos. Las decisiones
  recordadas «compra» se resetean igual para todo el negocio: son por esquema, no
  por archivo, y sólo precargan la pantalla.
- **``--revert <run_id>``:** revive los gastos que siguen anulados por esa corrida.

Nunca imprime la connection URL: sólo ``host`` y ``base``.

Usage (desde ``backend/``):
    DATABASE_URL='postgresql://...' .venv/bin/python scripts/reparar_stock_inicial_como_compra.py \
        --email x@y.com
    ... --tenant-id <uuid> [--file-id <uuid> ...] [--apply]
    ... --all                    (sólo dry-run)
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

from app.application.services import maintenance_lock_service as mls  # noqa: E402
from app.application.services.reparacion_stock_inicial_service import (  # noqa: E402
    PREFIJO,
    REPAIR_TYPE,
    Resumen,
    reparar,
    revertir,
)
from app.persistence.db.engine_factory import crear_engine  # noqa: E402
from app.persistence.models.repair import DataRepairRun  # noqa: E402
from app.persistence.models.transaction import ExpenseEntry  # noqa: E402

LEASE_TTL = 900
CREADO_POR = "script:reparar_stock_inicial_como_compra"


def _pesos(v: object) -> str:
    return "—" if v is None else f"${v:,.2f}"


def _imprimir(r: Resumen, aplicar: bool, nombre: str) -> None:
    print(f"\n── {nombre} ({str(r.tenant_id)[:8]}…)")
    print(f"   gastos a anular: {len(r.candidatos)} · {_pesos(r.monto)}")
    print(
        f"   de ellos en los últimos 30 días: {_pesos(r.monto_en_ventana(30))}"
        f" · 90 días: {_pesos(r.monto_en_ventana(90))}"
    )
    print(f"   flujo neto 30 d: {_pesos(r.flujo_antes)} → {_pesos(r.flujo_despues)}")
    for motivo, n in r.excluidos.most_common():
        print(f"   NO se tocan ({motivo}): {n} · {_pesos(r.monto_excluido[motivo])}")
    for a in r.archivos.values():
        print(
            f"   archivo {a.file_id} «{a.nombre}»: {a.gastos} gastos · {_pesos(a.monto)}"
            f" · elección guardada={a.eleccion!r}"
            + (f" · BLOQUEADO: {a.bloqueo}" if a.bloqueo else "")
        )
    print(
        f"   decisiones recordadas «compra» del negocio que pasan a apertura"
        f" (todas, no sólo las de estos archivos): {r.decisiones_compra}"
    )
    if aplicar and r.run_id:
        print(f"   APLICADO · run_id {r.run_id} (para revertir: --revert {r.run_id})")
        print(
            "   El score NO se recalcula solo: correr scripts/refresh_health_score.py"
            " con el email del negocio."
        )


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
        select(ExpenseEntry.tenant_id)
        .where(
            ExpenseEntry.voided_at.is_(None),
            ExpenseEntry.category == "INVENTORY",
            ExpenseEntry.description.like(f"{PREFIJO}%"),
        )
        .distinct()
    )
    return list(filas.scalars())


async def _con_lease(
    session: AsyncSession, tenant_id: uuid.UUID, motivo: str
) -> uuid.UUID | None:
    lease_id = await mls.acquire(
        session, tenant_id, reason=motivo, ttl_seconds=LEASE_TTL, created_by=CREADO_POR
    )
    await session.commit()
    if lease_id is None:
        print(f"⚠ tenant {tenant_id}: ya hay un lease de mantenimiento activo. SALTEADO.")
    return lease_id


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    quien = ap.add_mutually_exclusive_group(required=True)
    quien.add_argument("--tenant-id")
    quien.add_argument("--email")
    quien.add_argument("--all", action="store_true")
    quien.add_argument("--revert", metavar="RUN_ID")
    ap.add_argument("--file-id", action="append", default=None)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    if args.apply and (args.all or args.revert):
        print("ERROR: --apply va con --email o --tenant-id (un negocio por vez).")
        sys.exit(2)
    archivos = {uuid.UUID(f) for f in args.file_id} if args.file_id else None

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
                run_id = uuid.UUID(args.revert)
                run = await session.get(DataRepairRun, run_id)
                if run is None or run.repair_type != REPAIR_TYPE or run.tenant_id is None:
                    print(f"ERROR: {run_id} no es una corrida de {REPAIR_TYPE}.")
                    sys.exit(2)
                lease = await _con_lease(session, run.tenant_id, "mr_revert")
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
                    f"Revividos: {res.revividos} · salteados (cambiaron después):"
                    f" {res.salteados} · elecciones devueltas: {res.elecciones}"
                    f" · decisiones devueltas: {res.decisiones}"
                )
                return
            for tenant_id in await _tenants(session, args):
                nombre = (
                    await session.execute(
                        text("SELECT display_name FROM tenants WHERE tenant_id = :t"),
                        {"t": tenant_id},
                    )
                ).scalar_one_or_none() or "(sin nombre)"
                if not args.apply:
                    resumen = await reparar(session, tenant_id, aplicar=False, file_ids=archivos)
                    await session.rollback()
                    _imprimir(resumen, False, nombre)
                    continue
                lease = await _con_lease(session, tenant_id, "mr_stock_inicial")
                if lease is None:
                    continue
                try:
                    resumen = await reparar(session, tenant_id, aplicar=True, file_ids=archivos)
                    await session.commit()
                except BaseException:
                    # Sin esto, el release del ``finally`` corre sobre una
                    # transacción rota y tapa el error original.
                    await session.rollback()
                    raise
                finally:
                    await mls.release(session, lease)
                    await session.commit()
                _imprimir(resumen, True, nombre)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
