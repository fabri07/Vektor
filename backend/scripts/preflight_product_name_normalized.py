"""Preflight read-only para F8d — ``Product.name_normalized`` NOT NULL.

Reporta, SIN escribir nada, si el ``SET NOT NULL`` de la migración
``20260804_0001_product_name_normalized_notnull.py`` va a aplicar limpio contra
la base indicada por ``DATABASE_URL``. Corré esto contra Neon ANTES de mergear
o deployar esa migración.

La migración se auto-protege (aborta con ``RuntimeError`` si queda basura tras
el backfill), pero este script da el detalle accionable ANTES del deploy: qué
filas van a bloquear el ``SET NOT NULL`` y en qué tenant están, para repararlas
sin depender de leer el log de un deploy fallido.

Tres categorías (mismo criterio que ``_verify_clean`` de la migración):
  - ``name_normalized IS NULL``: fila legacy que el listener todavía no
    re-escribió (el backfill de la propia migración la resuelve sola).
  - ``name_normalized`` vacío tras trim: idem, distinto estado transitorio.
  - **Irresolubles**: el ``name`` crudo en sí es vacío/whitespace/ilegible —
    ``normalize_product_name(name)`` da ``""``. Estas NO las arregla el
    backfill (no-invention: no se inventa un nombre) y son las únicas que
    requieren reparación MANUAL antes del deploy.

Usage:
    DATABASE_URL='postgresql://...neon.tech/vektor?sslmode=require' \\
        .venv/bin/python scripts/preflight_product_name_normalized.py

ONLY runs SELECT statements. No writes. Nunca imprime la connection URL.
Correr desde backend/.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections import Counter
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _db import async_engine_config  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

from app.domain.text_norm import normalize_product_name  # noqa: E402

_CANDIDATES_SQL = text(
    "SELECT id, tenant_id, name FROM products "
    "WHERE name_normalized IS NULL OR trim(name_normalized) = '' "
    "ORDER BY tenant_id, id"
)


async def count_null(session: AsyncSession) -> int:
    """Cuenta filas con ``name_normalized IS NULL`` (straggler sin backfillear)."""
    result = await session.execute(
        text("SELECT count(*) FROM products WHERE name_normalized IS NULL")
    )
    return int(result.scalar_one())


async def count_empty(session: AsyncSession) -> int:
    """Cuenta filas con ``name_normalized`` vacío tras ``trim`` (pero no NULL)."""
    result = await session.execute(
        text(
            "SELECT count(*) FROM products "
            "WHERE name_normalized IS NOT NULL AND trim(name_normalized) = ''"
        )
    )
    return int(result.scalar_one())


async def find_candidates(session: AsyncSession) -> list[dict[str, Any]]:
    """Filas que impedirían el ``SET NOT NULL`` (espejo exacto de ``_verify_clean``
    de la migración): ``name_normalized`` NULL o vacío tras ``trim``."""
    rows = (await session.execute(_CANDIDATES_SQL)).mappings().all()
    return [dict(row) for row in rows]


def find_irresolubles(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """De los candidatos, los que el backfill de la migración NO puede resolver:
    el ``name`` crudo en sí normaliza a ``""`` (vacío/whitespace/ilegible). El
    resto de los candidatos se auto-resuelve solo con el backfill — no hace
    falta reparación manual para esos.
    """
    return [row for row in candidates if normalize_product_name(row["name"]) == ""]


def group_by_tenant(rows: list[dict[str, Any]]) -> Counter[str]:
    """Desglose por tenant — dónde reparar."""
    return Counter(str(row["tenant_id"]) for row in rows)


async def run_report(session: AsyncSession) -> None:
    total_null = await count_null(session)
    total_empty = await count_empty(session)
    candidates = await find_candidates(session)
    irresolubles = find_irresolubles(candidates)

    print(f"products.name_normalized IS NULL: {total_null}")
    print(f"products.name_normalized vacío (trim = ''): {total_empty}")
    print(f"Total candidatos al SET NOT NULL (backfilleables + irresolubles): {len(candidates)}")
    print(
        f"\nIrresolubles (name crudo vacío/whitespace/ilegible — no-invention, "
        f"requieren reparación manual del name): {len(irresolubles)}"
    )

    if not irresolubles:
        resolubles = len(candidates)
        if resolubles:
            print(
                f"\n{resolubles} fila(s) con name_normalized sucio pero name legible: "
                "el backfill de la propia migración las resuelve solas, no hace "
                "falta reparación manual."
            )
        print("\nEl SET NOT NULL de la migración 20260804_0001 aplicaría limpio.")
        return

    print("\nDesglose por tenant:")
    for tenant_id, count in group_by_tenant(irresolubles).most_common():
        print(f"  tenant_id={tenant_id}: {count}")

    print("\nDetalle (id, name crudo) — para ubicar y reparar antes del deploy:")
    for row in irresolubles:
        print(f"  id={row['id']}  tenant_id={row['tenant_id']}  name={row['name']!r}")


async def main() -> None:
    url, connect_args = async_engine_config()
    engine = create_async_engine(url, connect_args=connect_args)
    try:
        async with AsyncSession(engine) as session:
            await run_report(session)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
