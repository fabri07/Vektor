"""Fase 1.1 — medir dónde se va el tiempo del APPLY de relectura.

Corre un ``apply_reread`` REAL (no dry-run) sobre el Excel real de Asteria contra
Postgres LOCAL/descartable, cronometrado y **contando statements por forma de SQL**.
El conteo de statements es lo que importa: contra Postgres local la latencia por
statement es ~0,1 ms y contra Neon ~30-50 ms, así que los segundos locales son un
piso, no una estimación — pero un N+1 se ve igual en cualquiera de las dos.

Por qué hace falta medir: el apply de Asteria (6.103 filas de archivo, 2.563
registros a reemplazar) nunca completó en producción — 3 runs muertos entre el
14/8 y el 1/9 — y la task tiene ``time_limit=300``. Antes de subir el límite o
rediseñar, hay que saber si el tiempo es volumen legítimo o un N+1.

    Nota: el preview interactivo NO sirve como referencia. ``preview_reread`` usa
    ``_estimate_reread`` (estimación en memoria) y nunca llama a ``_reconcile``,
    así que su latencia no dice nada del costo del apply.

Uso:

    source <archivo con DATABASE_URL de Postgres LOCAL + S3_* de R2>
    .venv/bin/python scripts/bench_reread_apply.py            # siembra + mide
    .venv/bin/python scripts/bench_reread_apply.py --skip-seed  # re-mide sobre lo ya sembrado

Reusa el harness del Bloque 7 (`asteria_dryrun_bloque7.py`): mismo tenant/archivo
determinístico, misma descarga read-only de R2, y el mismo guard
``_abort_if_prod_like`` que aborta si ``DATABASE_URL`` huele a host administrado.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
import uuid
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Las flags de rollout se resuelven ANTES de importar el harness (que las prende
# con `setdefault` para su tenant). Default APAGADAS: el baseline que interesa es
# el camino que corre HOY en producción para Asteria, donde las tres están en [].
_ROLLOUT_VARS = (
    "PRODUCT_SUPPLIER_LINKS_ROLLOUT_TENANT_IDS",
    "CATALOG_FINAL_COST_ROLLOUT_TENANT_IDS",
    "INGESTION_SCHEMA_DECISIONS_ROLLOUT_TENANT_IDS",
)
if "--flags" not in sys.argv or "off" in sys.argv:
    for _v in _ROLLOUT_VARS:
        os.environ[_v] = ""

from scripts._bench_sql import PhaseTimer, SqlProfile, attach  # noqa: E402
from scripts.asteria_dryrun_bloque7 import (  # noqa: E402
    DRYRUN_TENANT_ID,
    FILENAME,
    _abort_if_prod_like,
    _build_confirm_payload,
    _download_asteria_file,
    _ensure_tenant,
    _ensure_uploaded_file,
    p,
)

# ── Instrumentación ───────────────────────────────────────────────────────────
#
# `SqlProfile`/`PhaseTimer`/`attach` viven en `_bench_sql` desde que el bench del
# confirm necesitó exactamente lo mismo: la conclusión de un bench depende de cómo
# agrupa el SQL, así que dos agrupadores distintos darían dos diagnósticos distintos
# sobre la misma corrida.

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

PROFILE = SqlProfile()
TIMER = PhaseTimer(PROFILE)


def _attach(engine: Any) -> None:
    attach(engine, PROFILE)


# ── Bench ─────────────────────────────────────────────────────────────────────


async def _seed(session: Any, summary: dict[str, Any], uploaded_file_id: uuid.UUID) -> None:
    """Deja la base en el mismo estado de partida que prod: el import previo que
    la relectura va a reemplazar (≈1.939 ventas / 624 gastos / 398 productos)."""
    from app.application.services.ingestion_import_service import insert_confirmed_data

    ctx_maps, ctx_entity, ctx_confirmed, stock_treat = await _build_confirm_payload(
        session, summary
    )
    t0 = time.perf_counter()
    counts = await insert_confirmed_data(
        session,
        DRYRUN_TENANT_ID,
        summary,
        {"productos": True, "ventas": True, "gastos": True},
        context_mappings=ctx_maps,
        context_entity=ctx_entity,
        context_confirmed=ctx_confirmed,
        stock_treatment=stock_treat,
        source="ingestion",
        uploaded_file_id=uploaded_file_id,
    )
    await session.commit()
    print(f"  import inicial en {time.perf_counter() - t0:.1f}s")
    for k, v in sorted(counts.items()):
        if isinstance(v, int | float) and v:
            print(f"    {k}: {v}")


async def _volumes(session: Any) -> dict[str, int]:
    from sqlalchemy import text

    vivo = "voided_at IS NULL"
    anulado = "voided_at IS NOT NULL"
    out: dict[str, int] = {}
    for label, sql in (
        ("ventas vivas", f"SELECT count(*) FROM sales_entries WHERE tenant_id=:t AND {vivo}"),
        (
            "ventas anuladas",
            f"SELECT count(*) FROM sales_entries WHERE tenant_id=:t AND {anulado}",
        ),
        ("gastos vivos", f"SELECT count(*) FROM expense_entries WHERE tenant_id=:t AND {vivo}"),
        (
            "gastos anulados",
            f"SELECT count(*) FROM expense_entries WHERE tenant_id=:t AND {anulado}",
        ),
        ("productos", "SELECT count(*) FROM products WHERE tenant_id=:t AND is_active"),
        (
            "movimientos vivos",
            f"SELECT count(*) FROM inventory_movements WHERE tenant_id=:t AND {vivo}",
        ),
        (
            "otros pendientes",
            "SELECT count(*) FROM unclassified_records WHERE tenant_id=:t AND status='PENDING'",
        ),
    ):
        out[label] = (await session.execute(text(sql), {"t": DRYRUN_TENANT_ID})).scalar() or 0
    return out


async def run(skip_seed: bool) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.application.services import reread_service
    from app.application.services.file_parsing import parse_uploaded_content
    from app.config.settings import get_settings
    from app.persistence.models.repair import DataRepairRun

    settings = get_settings()
    _abort_if_prod_like(settings.DATABASE_URL)
    _abort_if_prod_like(settings.DATABASE_URL_SYNC)

    p(f"BENCH APPLY — {settings.DATABASE_URL_SYNC.split('@')[-1]}")
    print(f"  flags de rollout: {[getattr(settings, v) for v in _ROLLOUT_VARS]}")

    engine = create_async_engine(
        settings.DATABASE_URL, pool_pre_ping=True, connect_args=settings.pg_connect_args
    )
    _attach(engine)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with session_maker() as session:
        await _ensure_tenant(session)
        content = await _download_asteria_file()
        uploaded_file_id = await _ensure_uploaded_file(session, len(content))
        summary = parse_uploaded_content(content, _XLSX_MIME, FILENAME)

        if not skip_seed:
            p("SEMBRANDO el import previo (no se mide)")
            await _seed(session, summary, uploaded_file_id)

        p("ESTADO DE PARTIDA")
        for k, v in (await _volumes(session)).items():
            print(f"  {k}: {v}")

        # Run equivalente al que deja `start_background_apply` antes de que el
        # worker lo reclame: mismo repair_type, dry_run=False, summary cacheado.
        run_row = DataRepairRun(
            tenant_id=DRYRUN_TENANT_ID,
            repair_type=reread_service.REPAIR_TYPE_REREAD,
            status="APPLYING",
            dry_run=False,
            details_json={"file_id": str(uploaded_file_id), "fresh_summary": summary},
        )
        session.add(run_row)
        await session.commit()

        from app.application.services import stock_service as _stock

        TIMER.wrap(reread_service, "_load_existing_records")
        TIMER.wrap(reread_service, "void_movement")
        TIMER.wrap(reread_service, "insert_confirmed_data")
        TIMER.wrap(_stock, "_get_or_create_balance")

        PROFILE.reset()
        PROFILE.enabled = True
        p("APLICANDO (apply_reread real, cronometrado)")
        t0 = time.perf_counter()
        result = await reread_service.apply_reread(
            session,
            uploaded_file_id,
            DRYRUN_TENANT_ID,
            run=run_row,
            fresh_override=summary,
        )
        t_reconcile = time.perf_counter() - t0
        stmts_reconcile = PROFILE.total

        t1 = time.perf_counter()
        await session.commit()
        t_commit = time.perf_counter() - t1
        PROFILE.enabled = False

        print(f"  voided={result.voided}  inserted={result.inserted}")
        print(f"\n  apply_reread : {t_reconcile:8.2f}s   {stmts_reconcile} statements")
        print(f"  commit final : {t_commit:8.2f}s")
        print(f"  TOTAL        : {t_reconcile + t_commit:8.2f}s")

        TIMER.report()
        PROFILE.report("STATEMENTS POR FORMA")

        p("ESTADO FINAL")
        for k, v in (await _volumes(session)).items():
            print(f"  {k}: {v}")

    await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--flags",
        choices=["on", "off"],
        default="off",
        help="flags de rollout de los Bloques 2/3A/5. off (default) = baseline de prod.",
    )
    parser.add_argument(
        "--skip-seed",
        action="store_true",
        help="no re-sembrar el import previo (re-medir sobre una base ya sembrada).",
    )
    args = parser.parse_args()
    asyncio.run(run(skip_seed=args.skip_seed))
