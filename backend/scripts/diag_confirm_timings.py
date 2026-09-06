"""Read-only: en qué ETAPA se fue el tiempo de un confirm de ingestión.

Uso:
    DATABASE_URL='postgresql://...neon.tech/vektor?sslmode=require' \
        .venv/bin/python scripts/diag_confirm_timings.py agustinalahora4@gmail.com

SOLO corre SELECT. No escribe nada. Nunca imprime la connection URL.

Por qué existe
--------------
El confirm publica su desglose por etapa desde F-T (``StageTimings`` →
``pipeline_events.detail.timings_ms``), pero nadie lo lee: la única cifra visible es
``latency_ms``, que mide **sólo** ``insert_confirmed_data`` y deja afuera las
validaciones pre-lease, el snapshot de maestros, el replay de inventario, la captura
a "Otros" y el aprendizaje de mapeos. Con ese número, "tardó 18 minutos" y
"latency_ms=40000" son las dos verdades a la vez y no hay por dónde seguir.

Antes de optimizar conviene saber **qué etapa** se comió el tiempo: un import lento y
un snapshot de maestros lento se arreglan en lugares distintos, y adivinar cuesta una
iteración entera de trabajo sobre el archivo equivocado.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

import asyncpg
from _db import normalize_dsn


def p(title: str) -> None:
    print(f"\n{'=' * 70}\n  {title}\n{'=' * 70}")


def _fmt_ms(ms: Any) -> str:
    try:
        v = int(ms)
    except (TypeError, ValueError):
        return str(ms)
    return f"{v / 1000:8.1f}s" if v >= 1000 else f"{v:7d}ms"


async def main(email: str, limite: int) -> None:
    raw = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_SYNC")
    if not raw:
        print("ERROR: exportá DATABASE_URL antes de correr.")
        sys.exit(2)
    dsn, sslarg = normalize_dsn(raw)
    conn = await asyncpg.connect(dsn, ssl=sslarg)
    try:
        await run(conn, email, limite)
    finally:
        await conn.close()


async def run(conn: asyncpg.Connection, email: str, limite: int) -> None:
    fila = await conn.fetchrow(
        "SELECT tenant_id FROM users WHERE lower(email) = lower($1) LIMIT 1", email
    )
    if not fila:
        print(f"  ⚠ No existe ningún usuario con email {email!r}.")
        return
    tenant_id = fila["tenant_id"]
    p(f"CONFIRMS DE {email}  (tenant {tenant_id})")

    eventos = await conn.fetch(
        # CONFIRM y REJECT: un confirm que explota es justamente donde más se necesita
        # saber dónde tardó, y su traza sale por el camino de rechazo, no por el feliz.
        "SELECT created_at, stage, file_id, rows_in, rows_out, rows_rejected, "
        "       latency_ms, detail "
        "FROM pipeline_events "
        "WHERE tenant_id = $1 AND stage IN ('CONFIRM', 'REJECT') "
        "ORDER BY created_at DESC LIMIT $2",
        tenant_id,
        limite,
    )
    if not eventos:
        print("  (sin eventos CONFIRM/REJECT registrados para este tenant)")
        return

    import json

    for ev in eventos:
        detail = ev["detail"]
        if isinstance(detail, str):
            try:
                detail = json.loads(detail)
            except ValueError:
                detail = {}
        detail = detail or {}
        timings = detail.get("timings_ms") or {}
        stages = timings.get("stages") or {}
        total = timings.get("total_ms")

        print(
            f"\n  {ev['created_at']:%Y-%m-%d %H:%M:%S}  {ev['stage']:<8} "
            f"file={str(ev['file_id'])[:8]}  filas={ev['rows_out']}  "
            f"latency_ms(import)={ev['latency_ms']}"
        )
        if total is not None:
            print(f"    TOTAL del confirm: {_fmt_ms(total)}")
        if not stages:
            # Los confirms anteriores a F-T no traen `timings_ms`: decirlo explícito
            # evita leer su ausencia como "no tardó nada".
            print("    (sin desglose por etapa — evento anterior a la instrumentación)")
            continue
        print(f"    {'ms':>10}  {'llamadas':>8}  {'filas':>8}  etapa")
        print(f"    {'-' * 10}  {'-' * 8}  {'-' * 8}  {'-' * 30}")
        for nombre, acc in sorted(stages.items(), key=lambda kv: -(kv[1].get("ms") or 0)):
            filas = acc.get("rows")
            print(
                f"    {_fmt_ms(acc.get('ms')):>10}  {acc.get('calls', 0):>8}  "
                f"{('-' if filas is None else filas):>8}  {nombre}"
            )
        if ev["stage"] == "REJECT" and detail.get("reason"):
            print(f"    motivo: {detail['reason']}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: diag_confirm_timings.py <email> [cantidad_de_eventos]")
        sys.exit(2)
    asyncio.run(main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 5))
