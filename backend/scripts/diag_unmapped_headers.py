"""Diagnóstico read-only: qué encabezados REALES no sabe leer el mapeo.

Recorre los ``parsed_summary_json`` de los archivos ya subidos, junta los
encabezados tal cual venían en las planillas y los pasa por la MISMA cadena
determinística que corre en producción (historial del tenant → reconocedor F-M →
fuzzy). Reporta los que quedan sin mapear, agrupados por frecuencia.

Existe porque cualquier lista de "encabezados que faltan" escrita a mano mide lo
que a alguien se le ocurrió, no lo que los negocios escriben. El propio
``test_header_corpus_vs_heuristics`` ya pagó esa lección.

La 4ª capa (LLM) NO se invoca: el objetivo es medir el techo determinístico, y
además el script no debe gastar tokens ni depender de una API key.

Uso:
    cd backend
    DATABASE_URL='postgresql://...neon.tech/vektor?sslmode=require' \
        .venv/bin/python scripts/diag_unmapped_headers.py

    # opcionales
    --tenant <uuid>     un solo tenant (default: todos)
    --top 60            cuántos encabezados listar (default 40)
    --out huecos.csv    además, volcar el detalle completo a CSV
    --incluir-mapeados  lista también los que SÍ resuelven (para revisar aciertos)

SOLO ejecuta SELECT. No escribe nada. Seguro contra producción.
NUNCA imprime la connection URL.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _db import normalize_dsn  # noqa: E402

from app.application.services.column_mapping_service import (  # noqa: E402
    _fuzzy_match,
    _normalize_col,
    read_header,
)
from app.domain.text_norm import repair_mojibake  # noqa: E402

#: entity_type → cómo lo llama el usuario, para el reporte.
_ENTIDAD_LABEL = {
    "sale": "ventas",
    "expense": "gastos",
    "product": "productos",
    "customer": "clientes",
    "supplier": "proveedores",
}


def p(title: str) -> None:
    print(f"\n{'=' * 74}\n  {title}\n{'=' * 74}")


def _resolver(header: str, entidad: str) -> tuple[str, str | None]:
    """(estado, target) por la cadena determinística — el mismo orden que el servicio.

    El historial del tenant queda AFUERA a propósito: mide el techo del
    vocabulario, no cuánto aprendió cada cuenta a fuerza de corregir a mano. Un
    alias aprendido tapa el hueco para ese tenant y lo deja abierto para todos
    los demás, que es justamente lo que se quiere contar.
    """
    normalizado = _normalize_col(repair_mojibake(header))
    lectura = read_header(normalizado, entidad)
    if lectura.outcome == "unico":
        return "mapeado", lectura.target
    if lectura.outcome == "ambiguo" or lectura.duda is not None:
        # El reconocedor SÍ entendió el encabezado y dice que no alcanza para
        # elegir. No es un hueco de vocabulario: es una pregunta legítima.
        return "ambiguo", None
    objetivo, _ratio = _fuzzy_match(normalizado, entidad)
    if objetivo is not None:
        return "fuzzy", objetivo
    return "sin_mapear", None


def _contextos(summary: Any) -> list[tuple[str, list[str]]]:
    """[(entity_type, headers)] de un parsed_summary_json, tolerante a formas viejas."""
    if not isinstance(summary, dict):
        return []
    salida: list[tuple[str, list[str]]] = []
    for ctx in summary.get("mapping_contexts") or []:
        if not isinstance(ctx, dict):
            continue
        entidad = ctx.get("entity_type")
        headers = ctx.get("headers")
        if entidad and isinstance(headers, list):
            salida.append((entidad, [str(h) for h in headers if h is not None]))
    if not salida:
        # Formato viejo (una tabla, sin mapping_contexts).
        headers = summary.get("headers")
        if isinstance(headers, list) and headers:
            entidad = _TIPO_A_ENTIDAD.get(str(summary.get("inferred_type") or ""))
            if entidad:
                salida.append((entidad, [str(h) for h in headers if h is not None]))
    return salida


_TIPO_A_ENTIDAD = {
    "ventas": "sale",
    "gastos": "expense",
    "stock": "product",
    "clientes": "customer",
    "proveedores": "supplier",
}


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tenant", default=None, help="UUID de un tenant (default: todos)")
    ap.add_argument("--top", type=int, default=40, help="cuántos encabezados listar")
    ap.add_argument("--out", default=None, help="volcar el detalle completo a este CSV")
    ap.add_argument(
        "--incluir-mapeados", action="store_true", help="listar también los que sí resuelven"
    )
    args = ap.parse_args()

    raw = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_SYNC")
    if not raw:
        print("ERROR: exportá DATABASE_URL antes de correr.")
        sys.exit(2)
    dsn, sslarg = normalize_dsn(raw)
    conn = await asyncpg.connect(dsn, ssl=sslarg)
    try:
        await run(conn, args)
    finally:
        await conn.close()


async def run(conn: asyncpg.Connection, args: argparse.Namespace) -> None:
    # `id` es la PK (UUIDPrimaryKeyMixin), no `file_id`.
    sql = (
        "SELECT tenant_id, id, original_filename, parsed_summary_json "
        "FROM uploaded_files "
        "WHERE parsed_summary_json IS NOT NULL AND deleted_at IS NULL"
    )
    params: list[Any] = []
    if args.tenant:
        sql += " AND tenant_id = CAST($1 AS uuid)"
        params.append(args.tenant)
    filas = await conn.fetch(sql, *params)

    p("COBERTURA DEL BARRIDO")
    print(f"  archivos con summary parseado: {len(filas)}")
    if not filas:
        print("  Nada que medir. ¿Filtro de tenant equivocado?")
        return

    import json as _json

    # (header_crudo, entidad) → veces que aparece
    ocurrencias: Counter[tuple[str, str]] = Counter()
    # (header, entidad) → estado
    estado: dict[tuple[str, str], tuple[str, str | None]] = {}
    # (header, entidad) → archivos donde aparece (para poder ir a mirarlos)
    archivos: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
    tenants_por_header: defaultdict[tuple[str, str], set[str]] = defaultdict(set)

    archivos_sin_contexto = 0
    for fila in filas:
        summary = fila["parsed_summary_json"]
        if isinstance(summary, str):
            try:
                summary = _json.loads(summary)
            except ValueError:
                continue
        contextos = _contextos(summary)
        if not contextos:
            archivos_sin_contexto += 1
            continue
        for entidad, headers in contextos:
            for h in headers:
                if not h.strip():
                    continue
                clave = (h, entidad)
                ocurrencias[clave] += 1
                archivos[clave].add(str(fila["original_filename"]))
                tenants_por_header[clave].add(str(fila["tenant_id"]))
                if clave not in estado:
                    estado[clave] = _resolver(h, entidad)

    print(f"  archivos sin encabezados legibles: {archivos_sin_contexto}")
    print(f"  columnas distintas (header, entidad): {len(estado)}")
    print(f"  apariciones totales de columna:       {sum(ocurrencias.values())}")

    por_estado: Counter[str] = Counter()
    apariciones_por_estado: Counter[str] = Counter()
    for clave, (est, _t) in estado.items():
        por_estado[est] += 1
        apariciones_por_estado[est] += ocurrencias[clave]

    p("RESULTADO POR ESTADO")
    print(f"  {'estado':12} {'columnas':>10} {'%':>7}   {'apariciones':>12} {'%':>7}")
    total_c = max(1, len(estado))
    total_a = max(1, sum(ocurrencias.values()))
    for est in ("mapeado", "fuzzy", "ambiguo", "sin_mapear"):
        c, a = por_estado.get(est, 0), apariciones_por_estado.get(est, 0)
        print(f"  {est:12} {c:>10} {c / total_c * 100:>6.1f}%   {a:>12} {a / total_a * 100:>6.1f}%")
    print(
        "\n  'ambiguo' NO es un hueco de vocabulario: el reconocedor entendió el\n"
        "  encabezado y dice que no alcanza para elegir. Se le pregunta al usuario."
    )

    p(f"SIN MAPEAR — top {args.top} por apariciones")
    huecos = [(k, v) for k, v in ocurrencias.items() if estado[k][0] == "sin_mapear"]
    huecos.sort(key=lambda kv: (-kv[1], kv[0][0].lower()))
    if not huecos:
        print("  (ninguno)")
    print(f"  {'veces':>6} {'tenants':>8}  {'entidad':11} encabezado")
    print(f"  {'-' * 6} {'-' * 8}  {'-' * 11} {'-' * 40}")
    for (h, entidad), veces in huecos[: args.top]:
        n_tenants = len(tenants_por_header[(h, entidad)])
        print(f"  {veces:>6} {n_tenants:>8}  {_ENTIDAD_LABEL.get(entidad, entidad):11} {h!r}")

    p("SIN MAPEAR — los que aparecen en MÁS DE UN tenant")
    print("  (un encabezado que repiten varios negocios es vocabulario que falta;\n"
          "   uno que usa un solo tenant puede ser una columna propia de esa cuenta)")
    compartidos = [
        (h, e, v)
        for (h, e), v in huecos
        if len(tenants_por_header[(h, e)]) > 1
    ]
    if not compartidos:
        print("\n  (ninguno)")
    for h, entidad, veces in compartidos[: args.top]:
        print(f"  {veces:>6} {len(tenants_por_header[(h, entidad)]):>8}  "
              f"{_ENTIDAD_LABEL.get(entidad, entidad):11} {h!r}")

    if args.incluir_mapeados:
        p("MAPEADOS — para revisar que los aciertos sean aciertos")
        ok = [(k, v) for k, v in ocurrencias.items() if estado[k][0] in ("mapeado", "fuzzy")]
        ok.sort(key=lambda kv: -kv[1])
        for (h, entidad), veces in ok[: args.top]:
            est, target = estado[(h, entidad)]
            print(f"  {veces:>6}  {_ENTIDAD_LABEL.get(entidad, entidad):11} {h!r:32} "
                  f"→ {target} ({est})")

    if args.out:
        destino = Path(args.out)
        with destino.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(
                ["header", "entidad", "estado", "target", "apariciones", "tenants", "archivos"]
            )
            for clave, veces in ocurrencias.most_common():
                h, entidad = clave
                est, target = estado[clave]
                w.writerow([
                    h, entidad, est, target or "", veces,
                    len(tenants_por_header[clave]),
                    " | ".join(sorted(archivos[clave])[:5]),
                ])
        print(f"\n  CSV escrito: {destino}  ({len(ocurrencias)} filas)")


if __name__ == "__main__":
    asyncio.run(main())
