"""M·P2a: arma el historial del costo promedio e imprime el informe de sombra.

El promedio es un DATO (el costo principal de cada venta sigue siendo el de
reposición) y en M·P2a no lo lee nadie: este script llena ``product_cost_history``
desde el ledger y mide, por negocio y por rubro, cuánto se separa el promedio de
la reposición. La regla vive en ``app/application/services/costo_promedio_service.py``.

- **Sin ``--apply`` (default) es un DRY-RUN:** reconstruye dentro de una
  transacción que se REVIERTE. Sirve sólo para el informe.
- **``--apply``:** guarda el historial. Sólo toca las tablas derivadas
  (``product_cost_history`` / ``product_cost_state``): ventas, gastos, stock y
  costos quedan como están. Reejecutarlo da lo mismo.

Hasta M·P2b (disparadores) el historial no se actualiza solo: una compra nueva
lo deja viejo hasta la próxima corrida.

Nunca imprime la connection URL: sólo ``host`` y ``base``.

Usage (desde ``backend/``):
    DATABASE_URL='postgresql://...' .venv/bin/python scripts/backfill_costo_promedio.py --all
    ... --tenant-id <uuid> [--apply]
    ... --email x@y.com [--apply]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import statistics
import sys
import uuid
from collections import Counter, defaultdict
from decimal import Decimal
from urllib.parse import urlparse

_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPTS)
sys.path.insert(0, os.path.dirname(_SCRIPTS))

from _db import async_engine_config  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker  # noqa: E402

from app.application.services.costo_promedio_service import Resumen, reconstruir  # noqa: E402
from app.persistence.db.engine_factory import crear_engine  # noqa: E402
from app.persistence.models.business import BusinessProfile  # noqa: E402
from app.persistence.models.product import Product  # noqa: E402


def _pct(v: Decimal | float | None) -> str:
    return "—" if v is None else f"{Decimal(str(v)) * 100:.1f}%"


def _brecha(brechas: list[Decimal]) -> str:
    if not brechas:
        return "sin productos medidos con reposición por compra"
    ordenadas = sorted(brechas)
    p90 = ordenadas[min(len(ordenadas) - 1, int(len(ordenadas) * 0.9))]
    return (
        f"mediana {_pct(statistics.median(ordenadas))} · p90 {_pct(p90)}"
        f" · {len(ordenadas)} productos"
    )


def _imprimir(r: Resumen, nombre: str, rubro: str) -> None:
    print(f"\n── {nombre} ({str(r.tenant_id)[:8]}…) · {rubro}")
    print(
        f"   productos {r.productos} · con historial {r.con_registros}"
        f" · registros {r.registros}"
    )
    estados = " · ".join(f"{k} {v}" for k, v in sorted(r.por_estado.items())) or "—"
    print(f"   último estado: {estados}")
    print(
        f"   aproximados (ventas importadas sin descontar stock): {r.aproximados}"
        f" · stock fuera del ledger: {r.fuera_del_ledger}"
    )
    print(f"   brecha (1 − promedio / reposición): {_brecha(r.brechas)}")


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
    return list((await session.execute(select(Product.tenant_id).distinct())).scalars())


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    quien = ap.add_mutually_exclusive_group(required=True)
    quien.add_argument("--tenant-id")
    quien.add_argument("--email")
    quien.add_argument("--all", action="store_true")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    url, connect_args = async_engine_config()
    p = urlparse(url)
    print(f"Base: host={p.hostname} base={(p.path or '/')[1:]}")
    print("MODO:", "APPLY" if args.apply else "DRY-RUN (no escribe)")
    engine = crear_engine(url, connect_args=connect_args, echo=False)
    fabrica = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    por_rubro: dict[str, list[Decimal]] = defaultdict(list)
    estados_rubro: dict[str, Counter[str]] = defaultdict(Counter)
    try:
        async with fabrica() as session:
            for tenant_id in await _tenants(session, args):
                nombre = (
                    await session.execute(
                        text("SELECT display_name FROM tenants WHERE tenant_id = :t"),
                        {"t": tenant_id},
                    )
                ).scalar_one_or_none() or "(sin nombre)"
                rubro = (
                    await session.execute(
                        select(BusinessProfile.vertical_code)
                        .where(BusinessProfile.tenant_id == tenant_id)
                        .limit(1)
                    )
                ).scalar_one_or_none() or "(sin rubro)"
                resumen = await reconstruir(session, tenant_id, medir_brecha=True)
                if args.apply:
                    await session.commit()
                else:
                    await session.rollback()
                _imprimir(resumen, nombre, rubro)
                por_rubro[rubro].extend(resumen.brechas)
                estados_rubro[rubro].update(resumen.por_estado)
        print("\n══ Por rubro")
        for rubro in sorted(por_rubro):
            estados = " · ".join(f"{k} {v}" for k, v in sorted(estados_rubro[rubro].items()))
            print(f"   {rubro}: brecha {_brecha(por_rubro[rubro])} | estados: {estados or '—'}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
