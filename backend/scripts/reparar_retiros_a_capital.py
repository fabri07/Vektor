"""D·3: los retiros viejos cargados como gasto pasan a ser retiros de capital.

La regla vive en ``app/application/services/reparacion_retiros_service.py``. Este
script elige el tenant, toma el lease de mantenimiento, imprime el resumen y
confirma o revierte.

- **Default: ANÁLISIS, sólo lectura** (``SELECT``, sin locks). Es lo que corre
  ``--all``: cuántos retiros, cuánto, por mes y por medio, editados y excluidos.
- **``--simular``** (un negocio): corre la conversión entera en una transacción,
  mide margen neto, Resultado y caja antes/después, y la REVIERTE. Toma el lock
  exclusivo de mantenimiento mientras dura: frena ventas y caja del local, así
  que se corre FUERA DE HORARIO, igual que ``--apply``.
- **``--apply``** (un negocio): convierte, en una sola transacción, cada gasto
  marcado en un ``retiro`` de capital en dinero (mismo importe, día y medio) y
  anula el gasto. Si la caja no da igual por día y medio, no se confirma nada.
- **``--desde``/``--hasta``** (AAAA-MM-DD) limitan por fecha de negocio del gasto.
- **``--revert <run_id>``:** deshace los pares que siguen como los dejó la
  corrida; los demás quedan pendientes y un nuevo ``--revert`` los reintenta.

Nunca imprime la connection URL: sólo ``host`` y ``base``.

Usage (desde ``backend/``):
    DATABASE_URL='postgresql://...' .venv/bin/python scripts/reparar_retiros_a_capital.py --all
    ... --email x@y.com [--desde 2026-01-01] [--hasta 2026-09-30] [--simular | --apply]
    ... --tenant-id <uuid> ...
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
from app.application.services.reparacion_retiros_service import (  # noqa: E402
    REPAIR_TYPE,
    CajaCambioError,
    Resumen,
    analizar,
    reparar,
    revertir,
)
from app.persistence.db.engine_factory import crear_engine  # noqa: E402
from app.persistence.models.repair import DataRepairRun  # noqa: E402
from app.persistence.models.transaction import ExpenseEntry  # noqa: E402

LEASE_TTL = 900
CREADO_POR = "script:reparar_retiros_a_capital"


def _pesos(v: object) -> str:
    return "—" if v is None else f"${v:,.2f}"


def imprimir(r: Resumen, modo: str, nombre: str) -> None:
    print(f"\n── {nombre} ({str(r.tenant_id)[:8]}…) · {modo}")
    print(f"   retiros a convertir: {len(r.candidatos)} · {_pesos(r.monto)}")
    if r.editados:
        print(f"   de ellos editados después del alta (entran con su valor actual): {r.editados}")
    for mes, monto in r.por_mes().items():
        print(f"   {mes}: el resultado de ese mes sube {_pesos(monto)}")
    for medio, monto in r.por_medio().items():
        print(f"   medio {medio}: {_pesos(monto)}")
    for motivo, n in r.excluidos.most_common():
        print(f"   NO se tocan ({motivo}): {n} · {_pesos(r.monto_excluido[motivo])}")
    for cuando in ("antes", "despues"):
        e = r.efecto.get(cuando)
        if e:
            print(
                f"   {cuando}: margen neto {e['margen_neto_periodo']} = {e['margen_neto_pct']}%"
                f" ({e['margen_neto_estado']}"
                + (f": {e['margen_neto_motivo']}" if e["margen_neto_motivo"] else "")
                + f") · Resultado del período {e['resultado_ars']}"
            )
    if r.efecto:
        print("   caja: igual por día y medio, en el arqueo y en la proyección")
    if modo == "APPLY" and r.run_id:
        print(f"   APLICADO · run_id {r.run_id} (para revertir: --revert {r.run_id})")
        print(
            "   El score NO se recalcula solo: correr scripts/refresh_health_score.py"
            " con el email del negocio."
        )


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
    # Todo tenant con gastos manuales vivos: la marca vive en un JSON y se filtra
    # en el servicio (no por categoría: un retiro editado pudo cambiarla).
    filas = await session.execute(
        select(ExpenseEntry.tenant_id)
        .where(ExpenseEntry.voided_at.is_(None), ExpenseEntry.source_upload_id.is_(None))
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


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    quien = ap.add_mutually_exclusive_group(required=True)
    quien.add_argument("--tenant-id")
    quien.add_argument("--email")
    quien.add_argument("--all", action="store_true")
    quien.add_argument("--revert", metavar="RUN_ID")
    modo = ap.add_mutually_exclusive_group()
    modo.add_argument("--simular", action="store_true")
    modo.add_argument("--apply", action="store_true")
    ap.add_argument("--desde")
    ap.add_argument("--hasta")
    return ap


def validar(args: argparse.Namespace) -> str | None:
    """El error de uso, o ``None``."""
    if (args.apply or args.simular) and (args.all or args.revert):
        return "ERROR: --simular y --apply van con --email o --tenant-id (un negocio por vez)."
    if args.revert and (args.desde or args.hasta):
        return "ERROR: --revert deshace la corrida entera: no lleva --desde/--hasta."
    return None


async def _revertir(session: AsyncSession, run_id: uuid.UUID) -> None:
    run = await session.get(DataRepairRun, run_id)
    if run is None or run.repair_type != REPAIR_TYPE or run.tenant_id is None:
        print(f"ERROR: {run_id} no es una corrida de {REPAIR_TYPE}.")
        sys.exit(2)
    lease = await _con_lease(session, run.tenant_id, "d3_revert")
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
    print(f"Revertidos: {len(res.revertidos)} · pendientes: {len(res.pendientes)}")
    for gasto, motivo in res.pendientes.items():
        print(f"   pendiente {gasto}: {motivo}")
    if res.revertidos:
        print(
            "El score NO se recalcula solo: correr scripts/refresh_health_score.py"
            " con el email del negocio."
        )
    if res.completa:
        print("Reversión COMPLETA.")
    elif res.reintentables:
        print(f"Quedan {len(res.reintentables)} pares que --revert puede reintentar.")
    else:
        print("Los pendientes no se reintentan (un usuario anuló el retiro migrado).")


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
    modo = (
        "REVERT" if args.revert
        else "APPLY" if args.apply
        else "SIMULACIÓN (revierte)" if args.simular
        else "ANÁLISIS (sólo lectura)"
    )
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
                if not (args.apply or args.simular):
                    resumen = await analizar(session, tenant_id, desde, hasta)
                    await session.rollback()
                    if resumen.candidatos or resumen.excluidos or not args.all:
                        imprimir(resumen, modo, nombre)
                    continue
                if args.simular:
                    try:
                        resumen = await reparar(
                            session, tenant_id, aplicar=False, desde=desde, hasta=hasta
                        )
                    except CajaCambioError as exc:
                        print(f"⚠ la caja cambiaría: {exc}. No se puede aplicar.")
                        continue
                    finally:
                        await session.rollback()
                    imprimir(resumen, modo, nombre)
                    continue
                lease = await _con_lease(session, tenant_id, "d3_retiros")
                if lease is None:
                    continue
                try:
                    resumen = await reparar(
                        session, tenant_id, aplicar=True, desde=desde, hasta=hasta
                    )
                    await session.commit()
                except CajaCambioError as exc:
                    await session.rollback()
                    print(f"⚠ {nombre}: la caja cambiaría ({exc}). NO se aplicó nada.")
                    continue
                except BaseException:
                    # Sin esto, el release del ``finally`` corre sobre una
                    # transacción rota y tapa el error original.
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
