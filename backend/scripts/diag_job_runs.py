"""Diagnóstico READ-ONLY de los jobs periódicos: ¿corren, cada cuánto, y salen bien?

Es la mitad automatizable de la verificación operativa que el programa dejó
pendiente (H16/E5). Responde con datos, no con logs:

* ¿hubo ejecuciones de este job en la ventana mirada?
* ¿con qué **cadencia observada**? Es lo que distingue un scheduler de una
  corrida manual: veinte ejecuciones separadas ~10 minutos las publica Beat, no
  una persona.
* ¿cuántas fallaron, y con qué error?

**Lo que NO demuestra.** Una fila en ``job_runs`` prueba que alguien EJECUTÓ la
task; no prueba quién la publicó. Un `celery call` a mano deja el mismo rastro
que Beat. Por eso el informe muestra la cadencia y los huecos —que es la
evidencia indirecta más fuerte disponible desde la base— y hay que completarlo
con el estado del despliegue del servicio de Beat. Tampoco demuestra nada sobre
un job que nunca se instrumentó: ausencia de filas es "no corrió **o** no
registra", y el script lo dice así.

SELECT-only. Nunca imprime la connection URL. Uso::

    export DATABASE_URL='postgresql://...'   # la provee el operador
    python scripts/diag_job_runs.py                       # últimas 24 h
    python scripts/diag_job_runs.py --horas 72 --job jobs.sweep_stale_reread_runs
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _db import normalize_dsn  # noqa: E402

#: Cada cuánto está programado cada job en `celery_app.beat_schedule`, en
#: segundos. Se compara contra la cadencia OBSERVADA: la diferencia entre "está
#: programado" y "está ocurriendo" es justamente lo que este script mide.
CADENCIA_ESPERADA = {"jobs.sweep_stale_reread_runs": 600}


def _p(titulo: str) -> None:
    print(f"\n{'=' * 70}\n  {titulo}\n{'=' * 70}")


async def run(conn: asyncpg.Connection, job: str, horas: int) -> int:
    existe = await conn.fetchval("SELECT to_regclass('public.job_runs') IS NOT NULL")
    if not existe:
        print(
            "La tabla job_runs no existe en esta base: falta aplicar la migración "
            "20260909_0001. Sin ella no hay traza que consultar."
        )
        return 2

    _p(f"{job} — últimas {horas} h")
    filas = await conn.fetch(
        """
        SELECT started_at, duration_ms, success, counters, error
        FROM job_runs
        WHERE job_name = $1 AND started_at >= now() - ($2 || ' hours')::interval
        ORDER BY started_at
        """,
        job,
        str(horas),
    )
    if not filas:
        print(
            "SIN EJECUCIONES REGISTRADAS.\n"
            "  No distingue entre «el job no corrió» y «el job corre pero no deja\n"
            "  traza» (sólo el barrido de relecturas está instrumentado). Si el job\n"
            "  es el barrido, esto es un hallazgo: hay que mirar el despliegue de\n"
            "  Beat y del worker que consume la cola `scores`."
        )
        return 1

    fallidas = [f for f in filas if not f["success"]]
    print(f"ejecuciones      : {len(filas)}  (fallidas: {len(fallidas)})")
    print(f"primera / última : {filas[0]['started_at']}  →  {filas[-1]['started_at']}")
    duraciones = sorted(f["duration_ms"] for f in filas)
    print(
        f"duración ms      : min {duraciones[0]}  mediana "
        f"{duraciones[len(duraciones) // 2]}  max {duraciones[-1]}"
    )

    _p("CADENCIA OBSERVADA")
    if len(filas) < 2:
        print("una sola ejecución: no alcanza para hablar de cadencia.")
    else:
        huecos = [
            (filas[i]["started_at"] - filas[i - 1]["started_at"]).total_seconds()
            for i in range(1, len(filas))
        ]
        huecos_ordenados = sorted(huecos)
        mediana = huecos_ordenados[len(huecos_ordenados) // 2]
        esperada = CADENCIA_ESPERADA.get(job)
        print(f"intervalo mediano: {mediana:.0f} s   (máximo: {max(huecos):.0f} s)")
        if esperada:
            print(f"programado cada  : {esperada} s")
            # Tolerancia amplia a propósito: lo que se quiere detectar es un
            # scheduler MUERTO o corridas manuales sueltas, no un jitter de
            # segundos que es normal en una cola compartida.
            if mediana > esperada * 2:
                print(
                    "  ⚠ la cadencia observada es MUY inferior a la programada: "
                    "revisá si Beat estuvo caído en la ventana."
                )
            else:
                print("  ✓ compatible con un scheduler publicando, no con corridas manuales")
        # Un hueco grande aislado no baja la mediana y es justo lo que hay que ver.
        largos = [h for h in huecos if esperada and h > esperada * 3]
        if largos:
            print(f"  ⚠ {len(largos)} hueco(s) de más de {esperada * 3} s entre ejecuciones")

    if fallidas:
        _p("FALLAS")
        for f in fallidas[-10:]:
            print(f"{f['started_at']}  {f['error']}")

    _p("ÚLTIMAS 5 EJECUCIONES")
    for f in filas[-5:]:
        estado = "ok " if f["success"] else "ERR"
        print(f"{f['started_at']}  {estado}  {f['duration_ms']:>6} ms  {f['counters']}")

    print(
        "\nRecordatorio: esto prueba que la task se EJECUTÓ, no quién la publicó. "
        "Completar con el estado del servicio de Beat."
    )
    return 0


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", default="jobs.sweep_stale_reread_runs")
    parser.add_argument("--horas", type=int, default=24)
    args = parser.parse_args()

    raw = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_SYNC")
    if not raw:
        print("ERROR: exportá DATABASE_URL antes de correr.")
        sys.exit(2)
    dsn, sslarg = normalize_dsn(raw)
    conn = await asyncpg.connect(dsn, ssl=sslarg)
    try:
        sys.exit(await run(conn, args.job, args.horas))
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
