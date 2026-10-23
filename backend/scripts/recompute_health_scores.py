"""Recalcula el health score de todos los tenants con las reglas actuales.

Usage:
    # Dry-run (default): dice cuántos tenants recalcularía y qué cambiaría en
    # una muestra, sin escribir nada.
    DATABASE_URL='postgresql://...' .venv/bin/python scripts/recompute_health_scores.py

    # Aplicar (a todos, o a un tenant puntual):
    ... scripts/recompute_health_scores.py --apply
    ... scripts/recompute_health_scores.py --apply --tenant <uuid>

Para qué existe
---------------
`data_completeness_score` ahora pondera por PROCEDENCIA del dato (observado vs
declarado en el onboarding). Los snapshots ya escritos en
`health_score_snapshots` conservan el valor viejo, y `/health-scores/latest`
lee de ahí — así que un tenant que no vuelva a escribir sigue mostrando su
confianza vieja para siempre.

`jobs.rebuild_weekly_history` haría exactamente esto todos los días, pero
**Beat no está desplegado como servicio** (ver CLAUDE.md), así que nadie lo
dispara en producción. Este script es el disparo manual de una sola vez
después del deploy.

Justamente los tenants que este cambio apunta —los que sólo contestaron la
encuesta— son los que nunca escriben, así que sin esta corrida el cambio no
los alcanza nunca.

Es idempotente: recalcular dos veces produce el mismo resultado. Los snapshots
son insert-only, así que cada corrida agrega una fila nueva por tenant y no
pisa el historial.

NUNCA imprime la connection URL. Requiere correr desde backend/.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _db import async_engine_config  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

# Cuántos tenants inspeccionar en el dry-run para mostrar el delta real. No se
# recalculan todos en seco porque `recalculate_for_tenant` escribe: el dry-run
# sólo lee lo que hay hoy.
MUESTRA_DRY_RUN = 20


async def _tenants_activos(session: AsyncSession, tenant: str | None) -> list[uuid.UUID]:
    if tenant:
        return [uuid.UUID(tenant)]
    rows = await session.execute(
        text(
            "SELECT tenant_id FROM tenants "
            "WHERE status IN ('active', 'trial') ORDER BY created_at"
        )
    )
    return [r[0] for r in rows.all()]


async def _estado_actual(session: AsyncSession, tids: list[uuid.UUID]) -> None:
    """Muestra el último snapshot vigente de una muestra — lo que ve el usuario hoy."""
    rows = await session.execute(
        text(
            """
            SELECT DISTINCT ON (s.tenant_id)
                   s.tenant_id, s.total_score, s.confidence_level,
                   s.data_completeness_score
              FROM health_score_snapshots s
             WHERE s.tenant_id = ANY(:tids)
             ORDER BY s.tenant_id, s.created_at DESC
            """
        ),
        {"tids": tids[:MUESTRA_DRY_RUN]},
    )
    filas = rows.all()
    if not filas:
        print("  (ninguno de la muestra tiene snapshot todavía)")
        return
    print(f"  Último snapshot vigente de {len(filas)} tenants (muestra):")
    for tid, total, conf, completeness in filas:
        marca = "  ← se va a mover" if completeness is not None and completeness >= 50 else ""
        print(f"    {tid}  score={total}  conf={conf}  completeness={completeness}{marca}")


async def main(apply: bool, tenant: str | None) -> None:
    # Lee DATABASE_URL del env y corta con mensaje claro si no está.
    url, connect_args = async_engine_config()
    engine = create_async_engine(url, pool_pre_ping=True, connect_args=connect_args)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    try:
        async with factory() as session:
            tids = await _tenants_activos(session, tenant)

        print(f"Tenants activos a recalcular: {len(tids)}")

        if not apply:
            async with factory() as session:
                await _estado_actual(session, tids)
            print("\nDRY-RUN: no se escribió nada. Reejecutá con --apply.")
            return

        # Import tardío: arrastra la app entera, y el dry-run no lo necesita.
        from app.application.services.health_score_service import (  # noqa: PLC0415
            HealthScoreService,
        )

        fallidos: list[uuid.UUID] = []
        for i, tid in enumerate(tids, start=1):
            # Sesión por tenant: el recálculo de uno NO puede llevarse puesto el
            # de los que vienen después (mismo aislamiento que rebuild_all_tenants).
            try:
                async with factory() as session:
                    svc = HealthScoreService(session)
                    await svc.recalculate_for_tenant(
                        tenant_id=tid,
                        triggered_by="manual_recompute:completeness_por_procedencia",
                    )
                    await session.commit()
            except Exception as exc:  # noqa: BLE001
                fallidos.append(tid)
                print(f"  [{i}/{len(tids)}] {tid}  FALLÓ: {exc}")
            else:
                print(f"  [{i}/{len(tids)}] {tid}  ok")

        print(f"\nListo. {len(tids) - len(fallidos)} recalculados, {len(fallidos)} fallidos.")
        if fallidos:
            print("Fallidos:", ", ".join(str(t) for t in fallidos))
            sys.exit(1)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="Escribir (default: dry-run)")
    ap.add_argument("--tenant", help="UUID de un tenant puntual (default: todos los activos)")
    args = ap.parse_args()
    asyncio.run(main(apply=args.apply, tenant=args.tenant))
