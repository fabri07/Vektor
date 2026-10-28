"""V-16: borra las huellas del lote de «Otros» que quedaron puestas sin importar nada.

La regla vive en ``app/application/services/reparacion_huellas_otros_service.py``.
Antes de #214 «Importar sugeridos» registraba la huella de la fila antes de las
compuertas, y una fila que quedaba en ``needs_manual`` salía «ya importada» para
siempre. Esto la libera para que el lote la vuelva a juzgar.

- **Default: ANÁLISIS, sólo lectura** (``SELECT``, sin locks): por negocio,
  cuántas filas PENDING tienen la huella, cuántas son huérfanas y cuántas tienen
  una venta o un gasto vivo (ésas nunca se tocan), con muestras.
- **``--apply``**: borra SÓLO las huérfanas. Una transacción por negocio, con los
  candados del lote (no frena las ventas del local) y una ``DataRepairRun``.
- **``--revert <run_id>``**: vuelve a poner las huellas de esa corrida.

Hay que decir a quién: ``--tenant-id`` o, explícito, ``--todos``. Nunca imprime la
connection URL: sólo ``host`` y ``base``.

Usage (desde ``backend/``):
    DATABASE_URL='postgresql://...' .venv/bin/python scripts/limpiar_huellas_otros.py --todos
    ... --tenant-id <uuid> [--apply]
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
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker  # noqa: E402

from app.application.services.reparacion_huellas_otros_service import (  # noqa: E402
    Resumen,
    analizar,
    reparar,
    revertir,
)
from app.persistence.db.engine_factory import crear_engine  # noqa: E402
from app.persistence.models.unclassified_record import (  # noqa: E402
    UNCLASSIFIED_STATUS_PENDING,
    UnclassifiedRecord,
)

CREADO_POR = "script:limpiar_huellas_otros"
MUESTRAS = 5


def imprimir(r: Resumen, modo: str) -> None:
    print(f"\n── tenant {r.tenant_id} · {modo}")
    print(
        f"   filas PENDING con huella del lote: {len(r.huerfanas) + len(r.con_respaldo)} · "
        f"huérfanas: {len(r.huerfanas)} · con venta/gasto vivo (no se tocan): "
        f"{len(r.con_respaldo)}"
    )
    for h in r.huerfanas[:MUESTRAS]:
        cuando = h.executed_at.date().isoformat() if h.executed_at else "—"
        print(f"   · {h.record_id} · {h.sugerida or '—'} · {h.motivo or '—'} · huella {cuando}")
    if modo == "APPLY" and r.run_id:
        print(f"   APLICADO · run_id {r.run_id} (para revertir: --revert {r.run_id})")


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    quien = ap.add_mutually_exclusive_group(required=True)
    quien.add_argument("--tenant-id")
    quien.add_argument("--todos", action="store_true")
    quien.add_argument("--revert", metavar="RUN_ID")
    ap.add_argument("--apply", action="store_true")
    return ap


def validar(args: argparse.Namespace) -> str | None:
    if args.apply and args.revert:
        return "ERROR: --revert deshace una corrida: no lleva --apply."
    return None


async def _tenants(session: AsyncSession, args: argparse.Namespace) -> list[uuid.UUID]:
    if args.tenant_id:
        return [uuid.UUID(args.tenant_id)]
    filas = await session.execute(
        select(UnclassifiedRecord.tenant_id)
        .where(UnclassifiedRecord.status == UNCLASSIFIED_STATUS_PENDING)
        .distinct()
    )
    return sorted(filas.scalars(), key=str)


async def correr(session: AsyncSession, args: argparse.Namespace) -> list[Resumen]:
    """El cuerpo del script sobre una sesión (los tests lo llaman sobre SQLite)."""
    if args.revert:
        try:
            repuestas = await revertir(session, uuid.UUID(args.revert))
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
        print(f"Huellas repuestas: {repuestas}")
        return []
    resumenes: list[Resumen] = []
    for tenant_id in await _tenants(session, args):
        if not args.apply:
            resumen = await analizar(session, tenant_id)
            await session.rollback()
            if resumen.huerfanas or resumen.con_respaldo or not args.todos:
                imprimir(resumen, "ANÁLISIS (sólo lectura)")
            resumenes.append(resumen)
            continue
        # Una transacción por negocio: si uno falla, los anteriores quedan hechos.
        try:
            resumen = await reparar(session, tenant_id, creado_por=CREADO_POR)
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
        imprimir(resumen, "APPLY")
        resumenes.append(resumen)
    return resumenes


async def main() -> None:
    args = parser().parse_args()
    error = validar(args)
    if error:
        print(error)
        sys.exit(2)
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
            await correr(session, args)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
