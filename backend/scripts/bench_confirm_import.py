"""Fase 1 — medir dónde se va el tiempo del CONFIRM de ingestión.

Corre un ``confirm_file`` REAL (el endpoint entero, no sólo ``insert_confirmed_data``)
sobre el Excel real de Asteria contra Postgres LOCAL/descartable, cronometrado y
**contando statements por forma de SQL**.

Por qué el endpoint entero y no el importador
---------------------------------------------
El confirm de Asteria tardó 18m33s para 2.994 filas (371 ms/fila). El bench de la
relectura ya había medido ``insert_confirmed_data`` sobre ESTE MISMO archivo en 2.096
statements — que no explican 18 minutos salvo con una latencia por statement absurda.
O sea: el cuello podía estar en cualquiera de las otras diez etapas del endpoint
(validaciones pre-lease, snapshot de maestros, replay de inventario, captura a
"Otros", ledger de reversa, aprendizaje de mapeos). Medir sólo el importador habría
respondido la pregunta equivocada.

Se llama a ``confirm_file`` como función y no por HTTP a propósito: el desglose por
etapa lo produce el propio endpoint (``StageTimings``), así que pasar por la capa
ASGI agregaría autenticación y routing —microsegundos— sin agregar información.

Uso:

    source <archivo con DATABASE_URL de Postgres LOCAL + S3_* de R2>
    .venv/bin/python scripts/bench_confirm_import.py              # siembra + mide
    .venv/bin/python scripts/bench_confirm_import.py --flags on   # con rollout activo
    .venv/bin/python scripts/bench_confirm_import.py --reset      # base limpia primero

``--reset`` borra los datos del tenant de dry-run (NO el tenant) para medir siempre el
mismo punto de partida: un import sobre una base ya poblada toma el camino de "producto
existente" y da otro número que uno sobre una base vacía. Sin fijar eso, dos corridas
del mismo código no son comparables.
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

# Las flags de rollout se resuelven ANTES de importar el harness (que las prende con
# `setdefault` para su tenant). Default APAGADAS: el baseline que interesa es el camino
# que corre HOY en producción para Asteria, donde las tres están en [].
_ROLLOUT_VARS = (
    "PRODUCT_SUPPLIER_LINKS_ROLLOUT_TENANT_IDS",
    "CATALOG_FINAL_COST_ROLLOUT_TENANT_IDS",
    "INGESTION_SCHEMA_DECISIONS_ROLLOUT_TENANT_IDS",
)
if "on" not in sys.argv:
    for _v in _ROLLOUT_VARS:
        os.environ[_v] = ""

# El bench imprime UNA tabla; el eco de SQL de `echo=settings.DEBUG` y el debug de
# botocore la sepultan bajo miles de líneas y vuelven ilegible justo el resultado.
# Se apagan acá y no en el env para que el script sirva igual desde cualquier shell.
import logging  # noqa: E402

for _ruidoso in ("sqlalchemy.engine", "botocore", "boto3", "urllib3", "s3transfer", "passlib"):
    logging.getLogger(_ruidoso).setLevel(logging.WARNING)
logging.getLogger("sqlalchemy.engine.Engine").propagate = False

from scripts._bench_sql import PhaseTimer, SqlProfile, attach, p  # noqa: E402
from scripts.asteria_dryrun_bloque7 import (  # noqa: E402
    DRYRUN_TENANT_ID,
    FILENAME,
    _abort_if_prod_like,
    _build_confirm_payload,
    _download_asteria_file,
    _ensure_tenant,
    _ensure_uploaded_file,
)

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

PROFILE = SqlProfile()
TIMER = PhaseTimer(PROFILE)

#: Tablas que el `--reset` vacía para este tenant. El orden importa: hijas antes que
#: padres, porque las FK son reales (no hay ON DELETE CASCADE en todas).
_TABLAS_A_LIMPIAR = (
    "inventory_movements",
    "inventory_balances",
    "product_supplier_links",
    "unclassified_records",
    "sales_entries",
    "expense_entries",
    "products",
    "customers",
    "suppliers",
    "operation_fingerprints",
    "pipeline_events",
    "decision_audit_log",
    "tenant_column_mappings",
)


async def _reset_datos(session: Any) -> None:
    from sqlalchemy import text

    for tabla in _TABLAS_A_LIMPIAR:
        await session.execute(
            text(f"DELETE FROM {tabla} WHERE tenant_id = :t"), {"t": DRYRUN_TENANT_ID}
        )
    await session.commit()


async def _volumes(session: Any) -> dict[str, int]:
    from sqlalchemy import text

    vivo = "voided_at IS NULL"
    out: dict[str, int] = {}
    for label, sql in (
        ("ventas vivas", f"SELECT count(*) FROM sales_entries WHERE tenant_id=:t AND {vivo}"),
        ("gastos vivos", f"SELECT count(*) FROM expense_entries WHERE tenant_id=:t AND {vivo}"),
        ("productos", "SELECT count(*) FROM products WHERE tenant_id=:t AND is_active"),
        (
            "  con description",
            "SELECT count(*) FROM products WHERE tenant_id=:t AND is_active "
            "AND description IS NOT NULL AND description <> ''",
        ),
        (
            "  con internal_sku",
            "SELECT count(*) FROM products WHERE tenant_id=:t AND is_active "
            "AND internal_sku IS NOT NULL",
        ),
        (
            "movimientos vivos",
            f"SELECT count(*) FROM inventory_movements WHERE tenant_id=:t AND {vivo}",
        ),
        ("balances", "SELECT count(*) FROM inventory_balances WHERE tenant_id=:t"),
        ("proveedores", "SELECT count(*) FROM suppliers WHERE tenant_id=:t"),
        (
            "  reales (no centinela)",
            "SELECT count(*) FROM suppliers WHERE tenant_id=:t "
            "AND coalesce(custom_fields->>'_sentinel','') <> 'true'",
        ),
        ("vinculos prod-prov", "SELECT count(*) FROM product_supplier_links WHERE tenant_id=:t"),
        ("clientes", "SELECT count(*) FROM customers WHERE tenant_id=:t"),
        (
            "otros pendientes",
            "SELECT count(*) FROM unclassified_records WHERE tenant_id=:t AND status='PENDING'",
        ),
    ):
        out[label] = (await session.execute(text(sql), {"t": DRYRUN_TENANT_ID})).scalar() or 0
    return out


def _wrap_fases() -> None:
    """Envuelve las funciones sospechosas EN EL MÓDULO QUE LAS IMPORTÓ."""
    from app.api.v1 import ingestion as api_ingestion
    from app.application.services import ingestion_import_service as imp
    from app.application.services import product_identity, stock_service

    TIMER.wrap(api_ingestion, "insert_confirmed_data", "endpoint→insert_confirmed_data")
    TIMER.wrap(api_ingestion, "snapshot_masters_before_import", "endpoint→snapshot_maestros")
    TIMER.wrap(api_ingestion, "build_master_details", "endpoint→build_master_details")
    TIMER.wrap(api_ingestion, "capture_column_risk_rows", "endpoint→captura_riesgo")
    TIMER.wrap(api_ingestion, "run_inventory_replay", "endpoint→replay_inventario")
    TIMER.wrap(imp, "add_product_or_reuse", "add_product_or_reuse")
    TIMER.wrap(imp, "_record_stock_movement", "_record_stock_movement")
    TIMER.wrap(imp, "_resolve_or_create_supplier", "_resolve_or_create_supplier")
    TIMER.wrap(imp, "_apply_purchase_to_stock", "_apply_purchase_to_stock")
    TIMER.wrap(imp, "_apply_catalog_stock", "_apply_catalog_stock")
    TIMER.wrap(imp, "build_incomplete_product", "build_incomplete_product")
    TIMER.wrap(product_identity, "add_product_or_reuse", "product_identity.add_or_reuse")
    TIMER.wrap(stock_service, "_get_or_create_balance", "stock._get_or_create_balance")


async def _preparar_archivo(session: Any, summary: dict[str, Any], file_id: uuid.UUID) -> None:
    """Deja el UploadedFile como lo dejaría el worker de parseo: summary persistido y
    estado NEEDS_CONFIRMATION (el confirm rechaza cualquier otro estado con 409)."""
    from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile

    record = await session.get(UploadedFile, file_id)
    record.parsed_summary_json = summary
    record.processing_status = PROCESSING_STATUS_NEEDS_CONFIRMATION
    record.import_lease_token = None
    record.import_lease_expires_at = None
    await session.commit()


def _armar_body(
    ctx_maps: dict[str, dict[str, str]],
    ctx_entity: dict[str, str],
    ctx_confirmed: dict[str, bool],
    stock_treat: dict[str, str],
) -> Any:
    """Traduce la salida del harness al body real del endpoint.

    `user_selected=True` en todas: el harness simula decisiones EXPLÍCITAS de la
    persona (es lo que hará el `ColumnMapperPanel` cuando el camino principal pase por
    él), y ese flag es lo que vuelve accionable un target opcional en el protocolo de
    riesgo de columnas.
    """
    from app.schemas.ingestion import ColumnMapping, ConfirmIngestionRequest

    mappings = [
        ColumnMapping(
            source_column=col,
            target_field=target,
            context_id=cid,
            entity_type=ctx_entity.get(cid),
            user_selected=True,
        )
        for cid, mapa in ctx_maps.items()
        for col, target in mapa.items()
    ]
    return ConfirmIngestionRequest(
        confirmed_fields={},
        column_mappings=mappings,
        context_confirmed=ctx_confirmed,
        context_entity=ctx_entity,  # type: ignore[arg-type]
        stock_treatment=stock_treat or None,
    )


async def run(reset: bool) -> None:
    from fastapi import BackgroundTasks
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    # `app.main` PRIMERO: `api/v1/ingestion` importa `limiter` de ahí, así que
    # importarlo suelto arranca la cadena al revés y explota con un circular
    # import a medio inicializar.
    import app.main  # noqa: F401
    from app.api.v1.ingestion import confirm_file
    from app.application.services.file_parsing import parse_uploaded_content
    from app.config.settings import get_settings
    from app.persistence.models.tenant import Tenant

    settings = get_settings()
    _abort_if_prod_like(settings.DATABASE_URL)
    _abort_if_prod_like(settings.DATABASE_URL_SYNC)

    p(f"BENCH CONFIRM — {settings.DATABASE_URL_SYNC.split('@')[-1]}")
    print(f"  flags de rollout: {[getattr(settings, v) for v in _ROLLOUT_VARS]}")

    engine = create_async_engine(
        settings.DATABASE_URL, pool_pre_ping=True, connect_args=settings.pg_connect_args
    )
    attach(engine, PROFILE)
    # `autoflush=False` como en producción (`app/persistence/db/session.py`): con
    # autoflush encendido, un SELECT dentro del import drena lo pendiente y el conteo
    # de statements sale distinto del que paga el usuario real.
    session_maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    async with session_maker() as session:
        await _ensure_tenant(session)
        if reset:
            p("RESET de datos del tenant de dry-run")
            await _reset_datos(session)
        content = await _download_asteria_file()
        file_id = await _ensure_uploaded_file(session, len(content))

        t0 = time.perf_counter()
        summary = parse_uploaded_content(content, _XLSX_MIME, FILENAME)
        t_parse = time.perf_counter() - t0
        print(f"  parseo: {t_parse:.1f}s")

        ctx_maps, ctx_entity, ctx_confirmed, stock_treat = await _build_confirm_payload(
            session, summary
        )
        _n_mapeos = sum(len(m) for m in ctx_maps.values())
        print(f"  contextos confirmados: {len(ctx_confirmed)}  mapeos: {_n_mapeos}")
        await _preparar_archivo(session, summary, file_id)

        p("ESTADO DE PARTIDA")
        for k, v in (await _volumes(session)).items():
            print(f"  {k}: {v}")

        _wrap_fases()
        body = _armar_body(ctx_maps, ctx_entity, ctx_confirmed, stock_treat)
        tenant = await session.get(Tenant, DRYRUN_TENANT_ID)

        PROFILE.reset()
        PROFILE.enabled = True
        p("CONFIRMANDO (confirm_file real, cronometrado)")
        t0 = time.perf_counter()
        resp = await confirm_file(
            file_id,
            body,
            BackgroundTasks(),
            tenant=tenant,
            session=session,
        )
        t_confirm = time.perf_counter() - t0

        # El commit lo hace la dependency `get_db_session`, que acá no corre: llamando
        # a `confirm_file` como función, sin este commit la transacción se descarta al
        # cerrar la sesión y una segunda corrida vuelve a medir un import EN FRÍO —
        # con lo cual el bench nunca probaría la idempotencia que dice probar.
        t1 = time.perf_counter()
        await session.commit()
        t_commit = time.perf_counter() - t1
        PROFILE.enabled = False

        print(f"  status={resp.status}  {resp.message}")
        for w in (resp.warnings or [])[:10]:
            print(f"  ⚠ {w}")
        print(f"\n  confirm_file : {t_confirm:8.2f}s")
        print(f"  commit final : {t_commit:8.2f}s")
        print(f"  TOTAL        : {t_confirm + t_commit:8.2f}s   {PROFILE.total} statements")

        timings = getattr(resp, "timings", None) or {}
        etapas = timings.get("stages") or {}
        if etapas:
            p("TIEMPO POR ETAPA (StageTimings del propio endpoint)")
            print(f"  {'ms':>9}  {'llamadas':>8}  {'filas':>8}  etapa")
            print(f"  {'-' * 9}  {'-' * 8}  {'-' * 8}  {'-' * 30}")
            for nombre, acc in sorted(etapas.items(), key=lambda kv: -(kv[1].get("ms") or 0)):
                filas = acc.get("rows")
                print(
                    f"  {acc.get('ms', 0):>9}  {acc.get('calls', 0):>8}  "
                    f"{('-' if filas is None else filas):>8}  {nombre}"
                )
            print(f"  total_ms del endpoint: {timings.get('total_ms')}")

        TIMER.report()
        PROFILE.report("STATEMENTS POR FORMA", limit=30)

        p("ESTADO FINAL")
        for k, v in (await _volumes(session)).items():
            print(f"  {k}: {v}")

    TIMER.restore()
    await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--flags",
        choices=["on", "off"],
        default="off",
        help="flags de rollout (product_supplier_links, catalog_final_cost, schema_decisions). "
        "off (default) = baseline de producción.",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="vaciar los datos del tenant de dry-run antes de medir (punto de partida fijo).",
    )
    args = parser.parse_args()
    asyncio.run(run(reset=args.reset))
