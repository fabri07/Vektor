"""Read-only global: detecta productos donde el COSTO pudo entrar como PRECIO DE VENTA.

CONTEXTO (incidente ASTERIA, 2026-07-31)
----------------------------------------
``_heuristic_match`` elige el keyword más largo y solo reemplaza si es
ESTRICTAMENTE mayor. Sobre el header ``precio_de_compra``, los keywords ``precio``
(6, ``sale_price_ars``) y ``compra`` (6, ``unit_cost_ars``) empataban en longitud
y ganaba el que se iterara primero — ``sale_price_ars``. La columna "Precio de
compra" se sugería como precio de venta, y si el usuario aceptaba la sugerencia el
costo entraba como precio: el margen de ese producto queda destruido y NADA en la
UI lo denuncia.

El fix (``_match_key``) corrige las sugerencias de acá en adelante. Este script
busca a quién le pasó ANTES.

**Solo detecta y reporta.** Reparar es human-in-the-loop, caso por caso: para
decidir hay que mirar el archivo fuente, y un "arreglo" automático que intercambie
precio y costo sobre un producto que en realidad se vende barato destruiría datos
buenos. Mismo criterio que ``detect_misvoided_purchases.py``.

JERARQUÍA DE EVIDENCIA
----------------------
Un mapeo aprendido NO demuestra que un producto concreto se haya importado con
él: pudo cambiar después, o haberse aprendido en otro archivo del mismo tenant.
Cada hallazgo se clasifica por la evidencia que efectivamente lo respalda:

1. ``confirmado`` — el ``STAGE_CONFIRM`` de ``pipeline_events`` de ESE archivo
   guarda el mapeo con el que se importó (``detail.mappings``), y ahí una columna
   con nombre de costo apunta a ``sale_price_ars``. Prueba directa. Solo existe
   para imports POSTERIORES al fix que agregó ese snapshot.
2. ``fuerte`` — el producto tiene ``source_row_ref``/``source_upload_id`` que lo
   atan a un archivo, y los headers reales de ese archivo
   (``parsed_summary_json``) incluyen una columna de costo. No prueba el mapeo,
   pero sí que el archivo de origen traía esa columna. Cubre el histórico.
3. ``probable`` — solo el patrón de valores (``unit_cost_ars IS NULL`` con un
   ``sale_price_ars`` sospechosamente bajo para el rubro) y/o un alias aprendido
   en ``tenant_column_mappings`` que manda costo → ``sale_price_ars``. Se reporta
   como PROBABLE, nunca como hecho.

El reporte incluye COBERTURA (revisados / sin evidencia / salteados), no solo
hallazgos: "0 hallazgos" y "no se pudo mirar" no son lo mismo.

READ-ONLY: cero UPDATE/INSERT sobre datos de negocio. La única escritura opcional
es el INSERT en ``decision_audit_log`` con ``--audit``. NUNCA imprime la
connection URL (la provee el usuario desde su shell).

Usage:
    DATABASE_URL='postgresql://...neon.tech/vektor?sslmode=require' \
        .venv/bin/python scripts/detect_cost_as_price_imports.py \
        --tenant ee2625dc-96b7-464c-bda3-7f7018cc2a5b --out cost_as_price.csv

    ... scripts/detect_cost_as_price_imports.py --all-active --out cost_as_price.csv
    ... scripts/detect_cost_as_price_imports.py --all-active --audit
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _db import async_engine_config, insert_decision_audit  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

_DECISION_TYPE = "COST_AS_PRICE_IMPORT_FINDING"
_TRIGGERED_BY = "script:detect_cost_as_price_imports"

# Fragmentos que delatan una columna de COSTO. Se comparan sobre el header
# normalizado igual que la heurística (lowercase + underscore), incluyendo la
# variante sin preposiciones ("precio de compra" → "precio_compra").
_COST_HINTS = (
    "costo",
    "compra",
    "cost",
    "unitario",
)

# Campo canónico al que NO debería haber ido una columna de costo.
_PRECIO_VENTA = "sale_price_ars"


def _norm(col: str) -> str:
    """Misma normalización que `column_mapping_service._normalize_col`."""
    return col.lower().strip().replace(" ", "_").replace("-", "_")


def _parece_costo(header: str) -> bool:
    n = _norm(header)
    # Se excluye explícitamente el header que SÍ es precio de venta aunque
    # contenga "unitario" u otra pista ("precio unitario de venta").
    if "venta" in n and "compra" not in n and "costo" not in n:
        return False
    return any(h in n for h in _COST_HINTS)


def _as_dict(value: Any) -> dict[str, Any] | None:
    """`detail`/`parsed_summary_json` llegan como dict (asyncpg) o str JSON."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _headers_del_summary(summary: dict[str, Any]) -> list[str]:
    """Todos los headers del archivo, de sus hojas o del nivel plano."""
    headers: list[str] = list(summary.get("headers") or [])
    for ctx in summary.get("mapping_contexts") or []:
        if isinstance(ctx, dict):
            headers.extend(ctx.get("headers") or [])
    return headers


def _mapeo_confirmado_manda_costo_a_precio(detail: dict[str, Any]) -> list[str]:
    """Columnas de costo que el mapeo CONFIRMADO mandó a `sale_price_ars`.

    Lee el snapshot que el confirm guarda desde el fix de traza. Es la única
    evidencia que prueba con qué mapeo se importó ESTE archivo.
    """
    mappings = detail.get("mappings")
    if not isinstance(mappings, dict):
        return []
    culpables: list[str] = []
    grupos: list[dict[str, Any]] = []
    if isinstance(mappings.get("flat"), dict):
        grupos.append(mappings["flat"])
    contexto = mappings.get("context")
    if isinstance(contexto, dict):
        grupos.extend(g for g in contexto.values() if isinstance(g, dict))
    for grupo in grupos:
        culpables.extend(
            col
            for col, target in grupo.items()
            if target == _PRECIO_VENTA and _parece_costo(str(col))
        )
    return culpables


async def _tenants(session: AsyncSession, args: argparse.Namespace) -> list[dict[str, Any]]:
    # `tenants` no tiene business_name/business_type/is_active: son
    # `display_name`/`legal_name` + `status`, y el rubro vive en
    # `business_profiles.vertical_code`. Mismo criterio de "activo" que
    # detect_misvoided_purchases.py: status IN ('ACTIVE','TRIAL').
    _select = (
        "SELECT t.tenant_id, t.display_name, bp.vertical_code "
        "FROM tenants t "
        "LEFT JOIN business_profiles bp ON bp.tenant_id = t.tenant_id "
    )
    if args.tenant:
        rows = await session.execute(
            text(_select + "WHERE t.tenant_id = CAST(:tid AS uuid)"),
            {"tid": args.tenant},
        )
    else:
        rows = await session.execute(
            text(_select + "WHERE t.status IN ('ACTIVE', 'TRIAL') ORDER BY t.created_at")
        )
    return [dict(r) for r in rows.mappings().all()]


async def _analizar_tenant(
    session: AsyncSession, tenant: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Devuelve (hallazgos, cobertura) de un tenant."""
    tid = str(tenant["tenant_id"])
    cobertura = {"revisados": 0, "sin_evidencia": 0, "hallazgos": 0}

    # Productos importados y activos. `unit_cost_ars IS NULL` es la firma del
    # problema: si la columna de costo se fue a `sale_price_ars`, no quedó nada
    # para el costo. Un producto con costo cargado NO está afectado.
    productos = (
        await session.execute(
            text(
                "SELECT id, name, sale_price_ars, unit_cost_ars, source_row_ref "
                "FROM products "
                "WHERE tenant_id = CAST(:tid AS uuid) "
                "  AND deactivated_at IS NULL "
                "  AND unit_cost_ars IS NULL "
                "  AND sale_price_ars > 0 "
                "  AND source_row_ref IS NOT NULL"
            ),
            {"tid": tid},
        )
    ).mappings().all()

    if not productos:
        return [], cobertura

    # ── Evidencia nivel 1: mapeos CONFIRMADOS de este tenant ──────────────────
    eventos = (
        await session.execute(
            text(
                "SELECT file_id, detail FROM pipeline_events "
                "WHERE tenant_id = CAST(:tid AS uuid) AND stage = 'confirm'"
            ),
            {"tid": tid},
        )
    ).mappings().all()
    archivos_confirmados: dict[str, list[str]] = {}
    for ev in eventos:
        detail = _as_dict(ev["detail"])
        if not detail or not ev["file_id"]:
            continue
        culpables = _mapeo_confirmado_manda_costo_a_precio(detail)
        if culpables:
            archivos_confirmados[str(ev["file_id"])] = culpables

    # ── Evidencia nivel 2: headers reales de los archivos del tenant ──────────
    archivos = (
        await session.execute(
            text(
                "SELECT id, original_filename, parsed_summary_json FROM uploaded_files "
                "WHERE tenant_id = CAST(:tid AS uuid)"
            ),
            {"tid": tid},
        )
    ).mappings().all()
    archivos_con_costo: dict[str, list[str]] = {}
    for arch in archivos:
        summary = _as_dict(arch["parsed_summary_json"]) or {}
        cols = [h for h in _headers_del_summary(summary) if _parece_costo(str(h))]
        if cols:
            archivos_con_costo[str(arch["id"])] = cols

    # ── Evidencia nivel 3: alias aprendidos que mandan costo → precio ─────────
    aliases = (
        await session.execute(
            text(
                "SELECT source_column FROM tenant_column_mappings "
                "WHERE tenant_id = CAST(:tid AS uuid) "
                "  AND entity_type = 'product' AND target_field = :target"
            ),
            {"tid": tid, "target": _PRECIO_VENTA},
        )
    ).mappings().all()
    alias_sospechosos = [
        str(a["source_column"]) for a in aliases if _parece_costo(str(a["source_column"]))
    ]

    hallazgos: list[dict[str, Any]] = []
    for prod in productos:
        cobertura["revisados"] += 1
        # `source_row_ref` ata el producto a la fila de un archivo. Los movimientos
        # de inventario guardan el upload; se usa para resolver de qué archivo vino.
        upload = (
            await session.execute(
                text(
                    "SELECT source_upload_id FROM inventory_movements "
                    "WHERE tenant_id = CAST(:tid AS uuid) AND product_id = CAST(:pid AS uuid) "
                    "  AND source_upload_id IS NOT NULL "
                    "ORDER BY created_at LIMIT 1"
                ),
                {"tid": tid, "pid": str(prod["id"])},
            )
        ).scalar()
        upload_id = str(upload) if upload else None

        nivel: str | None = None
        senal = ""
        if upload_id and upload_id in archivos_confirmados:
            nivel = "1-confirmado"
            senal = (
                "el mapeo confirmado de ese archivo mandó "
                f"{', '.join(archivos_confirmados[upload_id])} a precio de venta"
            )
        elif upload_id and upload_id in archivos_con_costo:
            nivel = "2-fuerte"
            senal = (
                "el archivo de origen traía la columna "
                f"{', '.join(archivos_con_costo[upload_id])} y el producto quedó sin costo"
            )
        elif alias_sospechosos:
            nivel = "3-probable"
            senal = (
                f"el tenant aprendió {', '.join(alias_sospechosos)} → precio de venta "
                "(no prueba que ESTE producto se haya importado así)"
            )

        if nivel is None:
            cobertura["sin_evidencia"] += 1
            continue

        cobertura["hallazgos"] += 1
        hallazgos.append(
            {
                "tenant_id": tid,
                "tenant": tenant.get("display_name") or "",
                "producto": prod["name"],
                "product_id": str(prod["id"]),
                "sale_price_ars": str(prod["sale_price_ars"]),
                "unit_cost_ars": "",
                "upload_id": upload_id or "",
                "nivel_evidencia": nivel,
                "senal": senal,
            }
        )

    return hallazgos, cobertura


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    grupo = parser.add_mutually_exclusive_group(required=True)
    grupo.add_argument("--tenant", help="tenant_id puntual")
    grupo.add_argument("--all-active", action="store_true", help="todos los activos")
    parser.add_argument("--out", help="CSV de salida")
    parser.add_argument(
        "--audit",
        action="store_true",
        help="además del reporte, registra los hallazgos en decision_audit_log",
    )
    args = parser.parse_args()

    url, connect_args = async_engine_config()
    engine = create_async_engine(url, connect_args=connect_args, echo=False)

    todos: list[dict[str, Any]] = []
    total = {"revisados": 0, "sin_evidencia": 0, "hallazgos": 0}
    salteados = 0

    async with AsyncSession(engine) as session:
        tenants = await _tenants(session, args)
        if not tenants:
            print("No se encontraron tenants para analizar.")
            await engine.dispose()
            return 1

        for tenant in tenants:
            try:
                hallazgos, cobertura = await _analizar_tenant(session, tenant)
            except Exception as exc:  # noqa: BLE001 — un tenant roto no corta el barrido
                salteados += 1
                print(f"  ! {tenant.get('display_name')}: no se pudo analizar ({exc})")
                continue
            todos.extend(hallazgos)
            for k in total:
                total[k] += cobertura[k]
            if hallazgos:
                print(
                    f"  {tenant.get('display_name')}: {len(hallazgos)} sospechoso(s) "
                    f"de {cobertura['revisados']} revisado(s)"
                )
            if args.audit and hallazgos:
                await insert_decision_audit(
                    session,
                    tenant_id=str(tenant["tenant_id"]),
                    decision_type=_DECISION_TYPE,
                    decision_data={
                        "hallazgos": hallazgos,
                        "cobertura": cobertura,
                        "nota": "solo detección; la reparación es manual, caso por caso",
                    },
                    triggered_by=_TRIGGERED_BY,
                )

        if args.audit:
            await session.commit()
        else:
            await session.rollback()

    await engine.dispose()

    # ── Reporte: cobertura ANTES que hallazgos ────────────────────────────────
    # "0 hallazgos" y "no se pudo mirar" no son lo mismo.
    print()
    print(f"Tenants analizados : {len(tenants) - salteados} (salteados: {salteados})")
    print(f"Productos revisados: {total['revisados']}")
    print(f"  sin evidencia    : {total['sin_evidencia']}")
    print(f"  sospechosos      : {total['hallazgos']}")
    por_nivel: dict[str, int] = {}
    for h in todos:
        por_nivel[h["nivel_evidencia"]] = por_nivel.get(h["nivel_evidencia"], 0) + 1
    for nivel in sorted(por_nivel):
        print(f"    {nivel}: {por_nivel[nivel]}")
    print()
    print("Este script NO repara. Revisá el archivo fuente de cada caso antes de tocar")
    print("nada: intercambiar precio y costo sobre un producto que realmente se vende")
    print("barato destruiría datos buenos.")

    if args.out and todos:
        with open(args.out, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(todos[0].keys()))
            writer.writeheader()
            writer.writerows(todos)
        print(f"\nCSV: {args.out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
