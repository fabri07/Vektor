"""F-CAT — completar la categoría de los productos que nacieron sin ella.

Contexto medido (Paso 0 del plan de ingesta, cuenta real): **0 de 398 productos
activos tenían categoría**. Son productos creados desde líneas de compra, donde
`build_incomplete_product` nacía con `category=None` y nadie lo completaba
después. Desde F-CAT los nuevos ya nacen con la categoría inferida; este script
es para los que ya están.

## Qué hace y qué NO hace

Infiere **sólo con evidencia**: la categoría se asigna únicamente si el nombre
del producto contiene el alias de **exactamente una** categoría del vertical
(`infer_product_category_from_name`). Nunca escribe `OTHER`: «Otros» es una
categoría real del catálogo y usarla como tacho dejaría el producto clasificado
sin que nadie lo haya clasificado, y encima invisible en el filtro «Sin
categoría», que es donde el usuario va a buscar lo que le falta completar.

Nunca pisa una categoría existente: sólo toca `category IS NULL` o vacío.

## Cobertura, no sólo cambios

El informe separa tres cosas que un contador de "cuántos toqué" mezcla:

    INFERIDO     → una sola categoría posible: se asigna (con --apply).
    AMBIGUO      → dos o más posibles ("alfombra felpuda exterior" es textil y de
                   exterior): NO se toca. Es trabajo humano, no una falla.
    SIN_EVIDENCIA→ ninguna palabra del nombre está en el vocabulario del vertical.
    YA_TENIA     → ya tenía categoría: no se toca (idempotencia).

Usage:
    # Dry-run (default), un tenant:
    DATABASE_URL='postgresql://...' .venv/bin/python \
        scripts/backfill_product_category.py --tenant <uuid>

    # Dry-run global + detalle a CSV:
    ... scripts/backfill_product_category.py --all-active --out categorias.csv

    # Aplicar, después de revisar el dry-run:
    ... scripts/backfill_product_category.py --tenant <uuid> --apply

REVERSIBLE: cada asignación queda auditada
(``decision_type='PRODUCT_CATEGORY_BACKFILL'``, con product_id + before/after).
Undo en bloque:

    UPDATE products SET category = NULL
      WHERE id IN (
        SELECT (decision_data->>'product_id')::uuid
        FROM decision_audit_log
        WHERE tenant_id = '<tid>' AND decision_type = 'PRODUCT_CATEGORY_BACKFILL'
      );

NUNCA imprime la connection URL. Read-only por default. Correr desde backend/.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
from collections import Counter
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _db import async_engine_config, insert_decision_audit  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

from app.domain.product_categories import (  # noqa: E402
    infer_product_category_from_name,
    product_category_candidates,
)
from app.domain.verticals import Vertical, parse_vertical  # noqa: E402

_INFERIDO = "INFERIDO"
_AMBIGUO = "AMBIGUO"
_SIN_EVIDENCIA = "SIN_EVIDENCIA"

_DECISION_TYPE = "PRODUCT_CATEGORY_BACKFILL"
_TRIGGERED_BY = "script:backfill_product_category"


async def _tenants(session: AsyncSession, args: argparse.Namespace) -> list[str]:
    if args.tenant:
        return [args.tenant]
    rows = await session.execute(
        text("SELECT tenant_id FROM tenants WHERE status = 'ACTIVE' ORDER BY tenant_id")
    )
    return [str(r[0]) for r in rows]


async def _vertical_de(session: AsyncSession, tenant_id: str) -> Vertical | None:
    row = (
        await session.execute(
            text(
                "SELECT vertical_code FROM business_profiles "
                "WHERE tenant_id = CAST(:tid AS uuid) LIMIT 1"
            ),
            {"tid": tenant_id},
        )
    ).first()
    if row is None or not row[0]:
        return None
    try:
        return parse_vertical(row[0])
    except Exception:  # noqa: BLE001
        # Un vertical desconocido no es motivo para adivinar con otro catálogo.
        return None


async def _procesar_tenant(
    session: AsyncSession, tenant_id: str, *, apply: bool
) -> tuple[Counter[str], list[dict[str, Any]]]:
    conteo: Counter[str] = Counter()
    detalle: list[dict[str, Any]] = []

    vertical = await _vertical_de(session, tenant_id)
    if vertical is None:
        print(f"  ⚠ tenant {tenant_id}: sin vertical reconocible → se saltea entero.")
        return conteo, detalle

    rows = await session.execute(
        text(
            "SELECT id, name FROM products "
            "WHERE tenant_id = CAST(:tid AS uuid) AND is_active "
            "  AND deactivated_at IS NULL "
            "  AND (category IS NULL OR btrim(category) = '') "
            "ORDER BY name"
        ),
        {"tid": tenant_id},
    )
    for pid, name in rows:
        candidatos = product_category_candidates(name, vertical)
        code = infer_product_category_from_name(name, vertical)
        if code is not None:
            estado = _INFERIDO
        elif len(candidatos) > 1:
            estado = _AMBIGUO
        else:
            estado = _SIN_EVIDENCIA
        conteo[estado] += 1
        detalle.append(
            {
                "tenant_id": tenant_id,
                "product_id": str(pid),
                "name": name,
                "estado": estado,
                "categoria": code or "",
                "candidatos": "|".join(sorted(candidatos)),
            }
        )
        if estado != _INFERIDO or not apply:
            continue
        await session.execute(
            text(
                "UPDATE products SET category = :cat "
                "WHERE id = CAST(:pid AS uuid) "
                # Guarda de carrera: si alguien la completó entre el SELECT y el
                # UPDATE, gana esa persona. El script nunca pisa una categoría.
                "  AND (category IS NULL OR btrim(category) = '')"
            ),
            {"cat": code, "pid": str(pid)},
        )
        await insert_decision_audit(
            session,
            tenant_id=tenant_id,
            decision_type=_DECISION_TYPE,
            decision_data={
                "product_id": str(pid),
                "name": name,
                "before": None,
                "after": code,
                "vertical": vertical.value,
            },
            triggered_by=_TRIGGERED_BY,
        )
    return conteo, detalle


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", help="UUID del tenant")
    parser.add_argument("--all-active", action="store_true", help="todos los ACTIVE")
    parser.add_argument("--apply", action="store_true", help="escribe (default: dry-run)")
    parser.add_argument("--out", help="detalle a CSV (.csv) o JSON")
    args = parser.parse_args()

    if not args.tenant and not args.all_active:
        print("ERROR: pasá --tenant <uuid> o --all-active.")
        sys.exit(2)

    url, connect_args = async_engine_config()
    engine = create_async_engine(url, connect_args=connect_args)
    total: Counter[str] = Counter()
    detalle: list[dict[str, Any]] = []
    try:
        async with AsyncSession(engine) as session:
            for tid in await _tenants(session, args):
                conteo, filas = await _procesar_tenant(session, tid, apply=args.apply)
                total.update(conteo)
                detalle.extend(filas)
            if args.apply:
                await session.commit()
    finally:
        await engine.dispose()

    modo = "APLICADO" if args.apply else "DRY-RUN (nada se escribió)"
    revisados = sum(total.values())
    print(f"\n{'=' * 70}\n  BACKFILL DE CATEGORÍAS — {modo}\n{'=' * 70}")
    print(f"  productos sin categoría revisados: {revisados}")
    for estado in (_INFERIDO, _AMBIGUO, _SIN_EVIDENCIA):
        n = total.get(estado, 0)
        pct = 100 * n // revisados if revisados else 0
        print(f"    {estado:<14} {n:>6}  ({pct:>3}%)")
    print(
        "\n  AMBIGUO no es una falla: son los que tienen más de una categoría\n"
        "  posible y los decide una persona. SIN_EVIDENCIA es vocabulario que\n"
        "  falta — si un rubro se repite, agregarlo a la tabla de alias del\n"
        "  vertical rinde más que completarlos a mano de a uno."
    )

    if args.out:
        if args.out.endswith(".csv"):
            with open(args.out, "w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(
                    fh,
                    fieldnames=[
                        "tenant_id",
                        "product_id",
                        "name",
                        "estado",
                        "categoria",
                        "candidatos",
                    ],
                )
                writer.writeheader()
                writer.writerows(detalle)
        else:
            with open(args.out, "w", encoding="utf-8") as fh:
                json.dump(detalle, fh, ensure_ascii=False, indent=2)
        print(f"\n  detalle escrito: {args.out}  ({len(detalle)} filas)")


if __name__ == "__main__":
    asyncio.run(main())
