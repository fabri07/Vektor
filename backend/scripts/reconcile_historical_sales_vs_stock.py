"""Read-only global: detecta productos donde las ventas (mayormente importadas
históricamente) SUPERAN el stock reconstruible al reproducir los eventos por FECHA DE
NEGOCIO. Es la versión temporal del chequeo agregado — cubre lo YA cargado.

CONTEXTO: F1 (venta en vivo) prohíbe el stock negativo hacia adelante (valida antes de
persistir). Pero las ventas importadas históricas nunca se validaron: al reconstruir la
línea de tiempo por ``occurred_at``/``transaction_date``, el balance puede caer bajo 0
(una venta datada ANTES que la compra que la cubre, o compra sin registrar). El chequeo
agregado (``inventory_integrity_service``) no ve la SECUENCIA; este script sí.

Reutiliza el mismo motor que el endpoint admin y el job semanal
(``inventory_temporal_service.check_products_temporal_divergence``) — cero lógica
duplicada. El anclaje confía en el VALOR del ancla ``catalog_initial_stock`` e IGNORA su
fecha (snapshot de import); desempate intra-día crédito-antes-que-débito (sesgo hacia
no-falso-positivo). Ver el docstring del motor para el detalle.

READ-ONLY: cero UPDATE/INSERT sobre datos de negocio; NUNCA escribe ``stock_units`` (la
corrección la decide un humano — regla de no-invención). La ÚNICA escritura permitida es
el INSERT opcional en ``decision_audit_log`` con ``--audit`` (un insert por tenant con
divergencias). NUNCA imprime la connection URL (la provee el usuario desde su shell).

CLASIFICACIÓN de causa (columna ``cause``):
- ``PURCHASES_DATED_AFTER_SALES``: el agregado da consistente (ending>=0) pero el path
  cayó negativo → las compras existen pero datadas después de las ventas = problema de
  FECHAS. Caso que el agregado no ve.
- ``NO_PURCHASES_OR_OVERSOLD``: ending<0 → faltan compras o sobreventa real (el agregado
  también lo marca; acá se agrega ``first_negative_at``).

Usage:
    # Un tenant puntual (empezar por acá; ej. don pedro)
    DATABASE_URL='postgresql://...neon.tech/vektor?sslmode=require' \
        .venv/bin/python scripts/reconcile_historical_sales_vs_stock.py \
        --tenant ee2625dc-96b7-464c-bda3-7f7018cc2a5b --out temporal_report.csv

    # Todos los tenants activos
    ... scripts/reconcile_historical_sales_vs_stock.py --all-active --out temporal_report.csv

    # Con auditoría (además del reporte)
    ... scripts/reconcile_historical_sales_vs_stock.py --all-active --audit
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import sys
import uuid
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _db import async_engine_config, insert_decision_audit  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

from app.application.services.inventory_temporal_service import (  # noqa: E402
    DECISION_TYPE_TEMPORAL,
    check_products_temporal_divergence,
)

_TRIGGERED_BY = "script:reconcile_historical_sales_vs_stock"

_CSV_FIELDS = [
    "tenant_id",
    "product_id",
    "product_name",
    "cause",
    "stock_units",
    "ending_balance",
    "opening_anchor_qty",
    "min_balance",
    "min_balance_at",
    "first_negative_at",
    "total_purchases",
    "total_tagged_adjustments",
    "total_loss",
    "total_sales",
    "event_count",
]


def _print_summary(tenant_id: str, divergences: list[dict[str, Any]]) -> None:
    print(f"\nTenant {tenant_id}: {len(divergences)} producto(s) con divergencia temporal")
    for d in divergences:
        print(
            f"  - {d['product_name']}: cae a {d['min_balance']} el "
            f"{d['first_negative_at']} (ending {d['ending_balance']}, {d['cause']})"
        )


def _write_report(path: str, rows: list[dict[str, Any]]) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nReporte escrito: {path} ({len(rows)} fila(s))")


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Detecta ventas (importadas) que exceden el stock reconstruible por fechas."
        )
    )
    parser.add_argument("--tenant", help="UUID de tenant puntual")
    parser.add_argument("--all-active", action="store_true", help="Todos los tenants activos")
    parser.add_argument(
        "--out",
        default="temporal_stock_report.csv",
        help="Path del reporte CSV (default temporal_stock_report.csv)",
    )
    parser.add_argument(
        "--audit",
        action="store_true",
        help="Insertar divergencias en decision_audit_log (única escritura permitida)",
    )
    args = parser.parse_args()

    if not args.tenant and not args.all_active:
        print("ERROR: indicá --tenant <uuid> o --all-active.")
        sys.exit(2)

    url, connect_args = async_engine_config()
    engine = create_async_engine(url, connect_args=connect_args)
    async with AsyncSession(engine) as session:
        if args.tenant:
            tids = [args.tenant]
        else:
            rows = await session.execute(
                # Canónico uppercase: la app solo escribe 'ACTIVE'/'TRIAL' (ver deps.py).
                text("SELECT tenant_id FROM tenants WHERE status IN ('ACTIVE', 'TRIAL')")
            )
            tids = [str(r[0]) for r in rows.all()]

        print(f"[READ-ONLY] reconciliación temporal de stock en {len(tids)} tenant(s)")

        all_rows: list[dict[str, Any]] = []
        audited_tenants = 0
        for tid in tids:
            result = await check_products_temporal_divergence(session, uuid.UUID(tid))
            divergences = result.divergences_as_dicts()
            if not divergences:
                continue
            _print_summary(tid, divergences)
            for d in divergences:
                all_rows.append({"tenant_id": tid, **d})
            if args.audit:
                await insert_decision_audit(
                    session,
                    tenant_id=tid,
                    decision_type=DECISION_TYPE_TEMPORAL,
                    decision_data={
                        "temporal_divergences": divergences,
                        "absolute_floor_units": result.absolute_floor_units,
                        "checked": result.checked,
                    },
                    triggered_by=_TRIGGERED_BY,
                )
                audited_tenants += 1

        if args.out:
            _write_report(args.out, all_rows)

        if args.audit:
            await session.commit()
            print(
                f"\nCOMMIT: {audited_tenants} tenant(s) auditado(s) "
                f"(decision_type={DECISION_TYPE_TEMPORAL}). Nada más se escribió."
            )
        else:
            await session.rollback()
            print(
                "\nDry read-only: nada se escribió. Usá --audit para dejar "
                "constancia en decision_audit_log."
            )

        print(
            "\nReparación: MANUAL, caso por caso. Una divergencia PURCHASES_DATED_AFTER_SALES "
            "suele ser una fecha de compra mal cargada; NO_PURCHASES_OR_OVERSOLD, una compra "
            "sin registrar. NUNCA recomputar stock_units desde el ledger (tiene base "
            "no-ledger); si hay que corregir, hacerlo INCREMENTAL y auditado."
        )
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
