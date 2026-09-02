"""Smoke de la relectura de Asteria en producción — READ-ONLY, solo SELECT.

Verifica, contra la base real, cada punto de la lista de validación del bloque
operativo 1. No dispara la relectura (eso lo hace el usuario desde la UI): mide
el ANTES y el DESPUÉS, y dice si cada criterio pasó o falló, con el dato crudo.

    .venv/bin/python scripts/smoke_reread_asteria.py --fase antes
    # ... el usuario corre UNA relectura desde la app ...
    .venv/bin/python scripts/smoke_reread_asteria.py --fase despues \
        --baseline /tmp/asteria_antes.json

NUNCA imprime la connection URL (ver ``scripts/_db``). No escribe nada.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

sys.path.insert(0, ".")
from scripts._db import async_engine_config  # noqa: E402

EMAIL = "agustinalahora4@gmail.com"

# Estado que el import del 10/8 dejó y que la relectura tiene que REEMPLAZAR
# (no sumar). Verificado read-only el 2026-09-01.
ESPERADO_VENTAS = 1939
ESPERADO_GASTOS = 624
ESPERADO_MOVIMIENTOS = 405

# Los 3 runs colgados que el sweeper tiene que llevar a estado terminal.
ZOMBIES = (
    "8516bebc-3f44-4b68-a435-7b9721262ed2",  # 14/8, RUNNING
    "62731cdf-af10-443c-bd9b-189531270fc9",  # 18/8, RUNNING
    "f98d7b04-eeeb-4ee0-8362-f4de0a3dd200",  # 1/9, APPLYING
)
TERMINALES = ("APPLIED", "FAILED", "COMPLETED", "REVERTED", "COMPLETED_WITH_ERRORS")


def p(t: str) -> None:
    print(f"\n{'=' * 76}\n  {t}\n{'=' * 76}")


def check(ok: bool, label: str, detalle: str = "") -> bool:
    print(f"  [{'OK ' if ok else 'FALLA'}] {label}{(' — ' + detalle) if detalle else ''}")
    return ok


async def _snapshot(c: Any, tid: str) -> dict[str, Any]:
    vivo, anul = "voided_at IS NULL", "voided_at IS NOT NULL"
    q = {
        "ventas_vivas": f"SELECT count(*) FROM sales_entries WHERE tenant_id=:t AND {vivo}",
        "ventas_anuladas": f"SELECT count(*) FROM sales_entries WHERE tenant_id=:t AND {anul}",
        "gastos_vivos": f"SELECT count(*) FROM expense_entries WHERE tenant_id=:t AND {vivo}",
        "gastos_anulados": f"SELECT count(*) FROM expense_entries WHERE tenant_id=:t AND {anul}",
        "movimientos_vivos": (
            f"SELECT count(*) FROM inventory_movements WHERE tenant_id=:t AND {vivo}"
        ),
        "productos": "SELECT count(*) FROM products WHERE tenant_id=:t AND is_active",
        "otros_pendientes": (
            "SELECT count(*) FROM unclassified_records WHERE tenant_id=:t AND status='PENDING'"
        ),
        "refs_venta_distintos": (
            "SELECT count(DISTINCT source_row_ref) FROM sales_entries "
            f"WHERE tenant_id=:t AND {vivo} AND source_row_ref IS NOT NULL"
        ),
        "refs_venta_duplicados": (
            "SELECT count(*) FROM (SELECT source_row_ref FROM sales_entries "
            f"WHERE tenant_id=:t AND {vivo} AND source_row_ref IS NOT NULL "
            "GROUP BY 1 HAVING count(*)>1) d"
        ),
        "refs_gasto_duplicados": (
            "SELECT count(*) FROM (SELECT source_row_ref FROM expense_entries "
            f"WHERE tenant_id=:t AND {vivo} AND source_row_ref IS NOT NULL "
            "GROUP BY 1 HAVING count(*)>1) d"
        ),
        "stock_total": "SELECT coalesce(sum(stock_units),0) FROM products WHERE tenant_id=:t",
    }
    return {k: (await c.execute(text(v), {"t": tid})).scalar() or 0 for k, v in q.items()}


async def _runs(c: Any, tid: str) -> list[dict[str, Any]]:
    rows = (
        await c.execute(
            text(
                "SELECT id, status, dry_run, created_at, updated_at, completed_at, "
                "  sales_detected, sales_voided, products_created, products_updated, "
                "  coalesce(details_json->>'reason','') AS reason, "
                "  coalesce(details_json->>'error','') AS error "
                "FROM data_repair_runs WHERE tenant_id=:t AND repair_type='REREAD_FILE' "
                "ORDER BY created_at DESC LIMIT 12"
            ),
            {"t": tid},
        )
    ).mappings().all()
    return [dict(r) for r in rows]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fase", choices=["antes", "despues"], required=True)
    ap.add_argument("--baseline", default="/tmp/asteria_antes.json")
    args = ap.parse_args()

    url, ca = async_engine_config()
    engine = create_async_engine(url, connect_args=ca)
    async with engine.connect() as c:
        tid = (
            await c.execute(text("SELECT tenant_id FROM users WHERE email=:e"), {"e": EMAIL})
        ).scalar()
        if tid is None:
            raise SystemExit("tenant no encontrado")
        tid = str(tid)
        snap = await _snapshot(c, tid)
        runs = await _runs(c, tid)

        p(f"SNAPSHOT ({args.fase})")
        for k, v in snap.items():
            print(f"  {k:<24} {v}")

        p("RUNS DE RELECTURA")
        for r in runs:
            print(
                f"  {r['id']} {r['status']:<10} dry={r['dry_run']!s:<5} "
                f"creado={r['created_at']} fin={r['completed_at']}"
            )
            if r["reason"] or r["error"]:
                print(f"      reason={r['reason']!r} error={r['error'][:160]!r}")
            if r["status"] == "APPLIED" or r["sales_voided"]:
                print(
                    f"      sales_detected={r['sales_detected']} "
                    f"sales_voided={r['sales_voided']} "
                    f"prod_created={r['products_created']} prod_updated={r['products_updated']}"
                )

        if args.fase == "antes":
            with open(args.baseline, "w") as f:
                json.dump({"snapshot": snap, "tenant_id": tid}, f, indent=2, default=str)
            print(f"\n  baseline guardado en {args.baseline}")
            await engine.dispose()
            return

        with open(args.baseline) as f:
            base = json.load(f)["snapshot"]

        p("VALIDACIÓN DEL BLOQUE OPERATIVO 1")
        ok = True
        aplicados = [r for r in runs if r["status"] == "APPLIED" and not r["dry_run"]]
        ok &= check(bool(aplicados), "hay al menos un run APPLIED")

        ok &= check(
            snap["ventas_vivas"] == ESPERADO_VENTAS,
            f"ventas vivas == {ESPERADO_VENTAS} (reemplazo, no suma)",
            f"vivas={snap['ventas_vivas']} (antes {base['ventas_vivas']})",
        )
        ok &= check(
            snap["ventas_anuladas"] - base["ventas_anuladas"] == ESPERADO_VENTAS,
            f"se anularon exactamente {ESPERADO_VENTAS} ventas",
            f"delta={snap['ventas_anuladas'] - base['ventas_anuladas']}",
        )
        ok &= check(
            snap["gastos_vivos"] == ESPERADO_GASTOS,
            f"gastos vivos == {ESPERADO_GASTOS}",
            f"vivos={snap['gastos_vivos']} (antes {base['gastos_vivos']})",
        )
        ok &= check(
            snap["gastos_anulados"] - base["gastos_anulados"] == ESPERADO_GASTOS,
            f"se anularon exactamente {ESPERADO_GASTOS} gastos",
            f"delta={snap['gastos_anulados'] - base['gastos_anulados']}",
        )
        ok &= check(
            snap["refs_venta_duplicados"] == 0 and snap["refs_gasto_duplicados"] == 0,
            "0 duplicados por source_row_ref",
            f"ventas={snap['refs_venta_duplicados']} gastos={snap['refs_gasto_duplicados']}",
        )
        ok &= check(
            snap["movimientos_vivos"] == ESPERADO_MOVIMIENTOS,
            f"movimientos vivos == {ESPERADO_MOVIMIENTOS} (stock no inflado)",
            f"movs={snap['movimientos_vivos']} stock={snap['stock_total']} "
            f"(antes {base['movimientos_vivos']}/{base['stock_total']})",
        )

        por_id = {str(r["id"]): r for r in runs}
        for z in ZOMBIES:
            zr = por_id.get(z)
            if zr is None:
                check(False, f"zombie {z[:8]} visible", "no apareció en los últimos 12 runs")
                continue
            ok &= check(
                zr["status"] in TERMINALES,
                f"zombie {z[:8]} en estado terminal",
                f"status={zr['status']} reason={zr['reason']!r}",
            )

        print()
        print("  RESULTADO:", "TODO OK" if ok else "HAY FALLAS — no cerrar el bloque")

    await engine.dispose()


asyncio.run(main())
