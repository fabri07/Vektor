"""Read-only: cuántas ediciones a mano dependen HOY de la posición de su fila.

Paso 0a de B·nuevo (``docs/plans/bnuevo-reemplazo-seguro-pr86.md``). Una edición a
mano sobre un registro importado se preserva en la relectura por su
``source_row_ref``, que es la POSICIÓN de la fila. Si las filas se corren, el freno
del #81 frena el apply. Antes de decidir si se automatiza algo (A/B/C) hacen falta
estos números, por tenant:

- registros vivos con ``has_user_edits`` que vienen de un archivo;
- cuántos tienen vínculo en ``operation_identity_links``, y de esos: identidad de
  LÍNEA (``#`` en la clave), sólo de DOCUMENTO, y COMPARTIDA (la identidad tiene más
  de un efecto vinculado);
- cuántos vienen de una fila con más de un efecto vivo (venta y gasto en la misma
  fila: comparten ``source_row_ref``);
- cuántos son legacy: sin ``source_row_ref``, o de un archivo cuyo resumen no trae
  ``mapping_contexts``;
- contextos totalmente editados;
- relecturas frenadas por el #81.

**Ausencia de dato ≠ cero.** Lo que la base no guarda se imprime como
``sin medición (<motivo>)``, nunca como 0.

SOLO SELECT, dentro de una transacción READ ONLY. NUNCA imprime la connection URL.

Usage:
    DATABASE_URL='postgresql://...' .venv/bin/python scripts/diag_ediciones_en_riesgo.py
    ... scripts/diag_ediciones_en_riesgo.py --tenant-id <uuid>
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any
from urllib.parse import urlparse

import asyncpg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _db import normalize_dsn  # noqa: E402

#: Registros vivos editados a mano que vienen de un archivo (venta o gasto).
_EDITADOS = """
    SELECT 'sale' AS tipo, id, tenant_id, source_upload_id, source_row_ref
      FROM sales_entries
     WHERE has_user_edits AND voided_at IS NULL AND source_upload_id IS NOT NULL
    UNION ALL
    SELECT 'expense', id, tenant_id, source_upload_id, source_row_ref
      FROM expense_entries
     WHERE has_user_edits AND voided_at IS NULL AND source_upload_id IS NOT NULL
"""

#: Todo efecto vivo de un archivo (para «fila con más de un efecto»).
_VIVOS = """
    SELECT 'sale' AS tipo, id, tenant_id, source_upload_id, source_row_ref
      FROM sales_entries WHERE voided_at IS NULL AND source_upload_id IS NOT NULL
    UNION ALL
    SELECT 'expense', id, tenant_id, source_upload_id, source_row_ref
      FROM expense_entries WHERE voided_at IS NULL AND source_upload_id IS NOT NULL
"""

_POR_TENANT = f"""
WITH ed AS ({_EDITADOS}),
vivos AS ({_VIVOS}),
vinc AS (
    SELECT l.entity_type, l.entity_id, i.identity_key,
           (SELECT count(*) FROM operation_identity_links o
             WHERE o.identity_id = l.identity_id) AS efectos
      FROM operation_identity_links l
      JOIN operation_identities i ON i.id = l.identity_id
     WHERE ($1::uuid IS NULL OR l.tenant_id = $1::uuid)
),
ed_vinc AS (
    SELECT ed.id,
           bool_or(v.identity_key LIKE '%#%') AS de_linea,
           bool_or(v.efectos > 1) AS compartida
      FROM ed JOIN vinc v ON v.entity_type = ed.tipo AND v.entity_id = ed.id
     GROUP BY ed.id
)
SELECT ed.tenant_id::text AS tenant_id,
       count(*) AS editados,
       count(ev.id) AS con_vinculo,
       count(*) FILTER (WHERE ev.de_linea) AS de_linea,
       count(*) FILTER (WHERE ev.id IS NOT NULL AND NOT ev.de_linea) AS solo_documento,
       count(*) FILTER (WHERE ev.compartida) AS compartida,
       count(*) FILTER (WHERE EXISTS (
           SELECT 1 FROM vivos o
            WHERE o.source_upload_id = ed.source_upload_id
              AND o.source_row_ref = ed.source_row_ref
              AND o.id <> ed.id)) AS fila_con_mas_de_un_efecto,
       count(*) FILTER (WHERE ed.source_row_ref IS NULL) AS sin_huella,
       count(*) FILTER (WHERE NOT (coalesce(f.parsed_summary_json, '{{}}'::jsonb)
                                   ? 'mapping_contexts')) AS sin_mapping_contexts
  FROM ed
  LEFT JOIN ed_vinc ev ON ev.id = ed.id
  LEFT JOIN uploaded_files f ON f.id = ed.source_upload_id
 WHERE ($1::uuid IS NULL OR ed.tenant_id = $1::uuid)
 GROUP BY ed.tenant_id
"""

#: Archivos con TODOS sus registros vivos editados. Es una cota inferior de los
#: contextos totalmente editados: el contexto no se guarda en el registro.
_ARCHIVOS_TODO_EDITADO = """
WITH v AS (
    SELECT tenant_id, source_upload_id, has_user_edits FROM sales_entries
     WHERE voided_at IS NULL AND source_upload_id IS NOT NULL
    UNION ALL
    SELECT tenant_id, source_upload_id, has_user_edits FROM expense_entries
     WHERE voided_at IS NULL AND source_upload_id IS NOT NULL
)
SELECT tenant_id::text AS tenant_id, count(*) AS archivos
  FROM (SELECT tenant_id, source_upload_id FROM v
         GROUP BY tenant_id, source_upload_id HAVING bool_and(has_user_edits)) t
 WHERE ($1::uuid IS NULL OR tenant_id = $1::uuid)
 GROUP BY tenant_id
"""

#: Relecturas con una medición del freno guardada en el run.
_FRENOS = """
SELECT tenant_id::text AS tenant_id,
       count(*) FILTER (WHERE status = 'READY_TO_APPLY') AS frenadas_vigentes,
       count(*) FILTER (WHERE status = 'READY_TO_APPLY'
                          AND coalesce((details_json->'data_loss'->>'edited_at_risk')::int, 0) > 0)
           AS vigentes_por_edicion,
       count(*) FILTER (WHERE status = 'APPLIED') AS aplicadas_aceptando,
       count(*) FILTER (WHERE status = 'APPLIED'
                          AND coalesce((details_json->'data_loss'->>'edited_at_risk')::int, 0) > 0)
           AS aceptadas_con_edicion,
       count(*) AS runs_con_medicion
  FROM data_repair_runs
 WHERE repair_type = 'REREAD_FILE'
   AND jsonb_typeof(details_json->'data_loss') = 'object'
   AND ($1::uuid IS NULL OR tenant_id = $1::uuid)
 GROUP BY tenant_id
"""

_SIN_TRAZA_DE_FRENOS = (
    "sin medición (cada freno no deja traza propia: el run guarda sólo la última "
    "medición y una sesión re-previsualizada o expirada la descarta; abajo, cotas "
    "inferiores de lo que sí quedó guardado)"
)
_SIN_CONTEXTO = (
    "sin medición (el contexto no se guarda en el registro: source_row_ref es un "
    "hash irreversible de hoja + fila; abajo, cota inferior por archivo entero)"
)


def informe(
    base: str,
    tenant: str | None,
    por_tenant: dict[str, Any],
    todo_editado: dict[str, int],
    frenos: dict[str, Any],
) -> list[str]:
    """Las líneas del reporte. Pura: se prueba sin base.

    Sin ningún tenant con datos igual imprime un bloque entero: los conteos que la
    base SÍ guarda salen en 0 (cuenta hecha) y lo que no guarda sale «sin medición».
    Un reporte vacío no puede afirmar «ni relecturas frenadas».
    """
    out = [f"Base leída: {base}"]

    def linea(etiqueta: str, valor: Any) -> None:
        out.append(f"  {etiqueta:<52}: {valor}")

    tenants: list[str | None] = [
        *sorted(set(por_tenant) | set(frenos) | ({tenant} if tenant else set()))
    ] or [None]
    for tid in tenants:
        titulo = (
            f"tenant {tid}" if tid else
            "todos los tenants (ninguno con registros editados ni runs con medición)"
        )
        out.append(f"\n{'=' * 78}\n  {titulo}\n{'=' * 78}")
        r = por_tenant.get(tid) if tid else None
        if r is None:
            linea("registros editados a mano de archivos importados", 0)
        else:
            linea("registros editados a mano de archivos importados", r["editados"])
            linea("  con vínculo en operation_identity_links", r["con_vinculo"])
            linea("    identidad de línea", r["de_linea"])
            linea("    sólo de documento", r["solo_documento"])
            linea("    identidad compartida con otros efectos", r["compartida"])
            linea("  de una fila con más de un efecto vivo", r["fila_con_mas_de_un_efecto"])
            linea("  legacy: sin source_row_ref", r["sin_huella"])
            linea("  legacy: archivo sin mapping_contexts", r["sin_mapping_contexts"])
        linea("contextos totalmente editados", _SIN_CONTEXTO)
        linea("  archivos con todo lo vivo editado (cota inferior)", todo_editado.get(tid or "", 0))
        linea("relecturas frenadas por el #81", _SIN_TRAZA_DE_FRENOS)
        f = (frenos.get(tid) if tid else None) or {}
        linea("  frenadas vigentes (READY_TO_APPLY con medición)", f.get("frenadas_vigentes", 0))
        linea("    de ellas, por edición en riesgo", f.get("vigentes_por_edicion", 0))
        linea("  aplicadas aceptando la pérdida medida", f.get("aplicadas_aceptando", 0))
        linea("    de ellas, con edición en riesgo", f.get("aceptadas_con_edicion", 0))
    out.append("")
    out.append("Lectura: una edición sin vínculo de identidad de LÍNEA sólo se puede ubicar")
    out.append("por su posición. Un 0 es una cuenta hecha; 'sin medición' es un dato que")
    out.append("la base no guarda.")
    return out


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", help="un solo tenant (sin esto: todos)")
    args = parser.parse_args()

    raw = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_SYNC")
    if not raw:
        print("ERROR: exportá DATABASE_URL antes de correr.")
        return 2
    dsn, sslarg = normalize_dsn(raw)
    conn = await asyncpg.connect(dsn, ssl=sslarg)
    try:
        async with conn.transaction(readonly=True):
            tenant = args.tenant_id
            # Host y nombre de base, nunca usuario ni contraseña.
            nombre = await conn.fetchval("SELECT current_database()")
            base = f"host={urlparse(dsn).hostname} base={nombre}"
            por_tenant = {r["tenant_id"]: r for r in await conn.fetch(_POR_TENANT, tenant)}
            filas = await conn.fetch(_ARCHIVOS_TODO_EDITADO, tenant)
            todo_editado = {r["tenant_id"]: r["archivos"] for r in filas}
            frenos = {r["tenant_id"]: r for r in await conn.fetch(_FRENOS, tenant)}
    finally:
        await conn.close()

    print("\n".join(informe(base, tenant, por_tenant, todo_editado, frenos)))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
