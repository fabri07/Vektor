"""Preflight read-only de la unificación de códigos de vertical.

Reporta, SIN escribir nada, cómo va a caer la migración
``20260806_0002_unify_vertical_codes.py`` contra la base indicada por
``DATABASE_URL``. Corré esto contra Neon ANTES de mergear o deployar.

Qué imprime
-----------
1. Histograma de ``business_profiles.vertical_code`` — el que leen heurísticas,
   categorías, FactsService y el health engine.
2. Histograma de ``analytics_events.vertical_code`` — si queda partido entre el
   código corto y el largo, ``AnalyticsRepository.compute_margin_benchmark``
   nunca alcanza su ``min_samples=5`` y los benchmarks data-driven dejan de
   computarse en silencio.
3. Histograma de ``heuristic_rule_sets.vertical``.
4. Histograma de ``vertical_field_definitions.vertical_code`` — el catálogo base
   de campos por rubro. ``field_definition_service`` filtra por IGUALDAD
   (``VerticalFieldDefinition.vertical_code == vertical_code``), así que una fila
   con el código corto no explota: devuelve **menos definiciones, en silencio**,
   que es exactamente el modo de falla que este PR vino a borrar. La probabilidad
   de que haya filas legadas es baja (``scripts/seed_vertical_fields.py`` deriva
   el código del nombre de archivo y los tres JSON de
   ``app/application/data/vertical_fields/`` ya usan códigos largos), pero la
   corrida contra Neon es de UNA sola oportunidad: si no se pregunta acá, no nos
   enteramos nunca.
5. **Tenants SIN ``BusinessProfile``.** Ese número es el agujero de los signups
   viejos por Google (``google_oauth_service._create_social_user`` no crea
   perfil): hasta las Tasks 2-3 sobrevivían gracias a los fallbacks silenciosos
   a kiosco; ahora que el código parsea estricto, cada uno de esos tenants es un
   estado roto real. Se reparan de a uno con
   ``scripts/backfill_missing_business_profiles.py``, que EXIGE el vertical (no
   lo adivina).

Cada histograma marca cada código como ``[canónico]``, ``[legado → X]`` (la
migración lo reescribe sola), ``[sentinela histórico]`` o ``[DESCONOCIDO]`` (hay
que decidir a mano a qué rubro corresponde).

Solo los desconocidos de ``business_profiles`` BLOQUEAN el deploy: es la única
tabla sobre la que corren ``_verify_verticals_canonical`` (que aborta con
``RuntimeError``) y el CHECK final. Un desconocido en las otras tres no corta el
deploy, pero sí degrada en silencio (benchmarks data-driven, catálogo de campos),
así que también se reporta.

**Este script INFORMA, no repara.** En particular
``vertical_field_definitions`` NO entra al ``UPDATE`` de la migración: su clave
lógica es compuesta (``vertical_code``, ``field_key``, ``entity_type``), así que
una reescritura ciega podría colisionar con filas ya canónicas. Primero se mira
el histograma; si aparece algo, se decide a mano.

``heuristic_rule_sets.vertical = 'ALL'`` NO es un código roto: es el ruleset base
"para todos los verticales" que sembró la migración inicial
(``20260310_0001_initial_schema_v11.py``). No se migra ni se cuenta como
ofensor.

Usage:
    DATABASE_URL='postgresql://...neon.tech/vektor?sslmode=require' \\
        .venv/bin/python scripts/preflight_vertical_codes.py

ONLY runs SELECT statements. No writes. Nunca imprime la connection URL.
Correr desde backend/.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _db import async_engine_config  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

from app.domain.verticals import OPERATIONAL_VERTICALS  # noqa: E402

#: Alias legado → código canónico. Espejo (invertido) de ``_ALIAS_LEGADOS`` de la
#: migración ``20260806_0002``: acá solo sirve para ANOTAR el reporte, la
#: reescritura real la hace la migración.
LEGACY_TO_CANONICAL: dict[str, str] = {
    "kiosco": "kiosco_almacen",
    "almacen": "kiosco_almacen",
    "deco": "decoracion_hogar",
    "deco_hogar": "decoracion_hogar",
    "decoracion": "decoracion_hogar",
    "cleaning": "limpieza",
}

#: Valores que NO son un rubro pero son legítimos en su tabla. ``'ALL'`` es el
#: ruleset base "para todos los verticales" que sembró la migración inicial
#: ``20260310_0001_initial_schema_v11.py``; ni la migración lo toca ni cuenta
#: como ofensor.
SENTINELAS_POR_TABLA: dict[str, frozenset[str]] = {
    "heuristic_rule_sets": frozenset({"ALL"}),
}

#: (etiqueta, tabla, columna, ¿un desconocido acá bloquea el deploy?). Solo
#: ``business_profiles`` bloquea: es la única tabla que miran
#: ``_verify_verticals_canonical`` y el CHECK de la migración.
#:
#: ``vertical_field_definitions`` se REPORTA pero la migración NO la reescribe
#: (clave lógica compuesta — ver docstring del módulo).
VERTICAL_COLUMNS: tuple[tuple[str, str, str, bool], ...] = (
    ("business_profiles.vertical_code", "business_profiles", "vertical_code", True),
    ("analytics_events.vertical_code", "analytics_events", "vertical_code", False),
    ("heuristic_rule_sets.vertical", "heuristic_rule_sets", "vertical", False),
    (
        "vertical_field_definitions.vertical_code",
        "vertical_field_definitions",
        "vertical_code",
        False,
    ),
)


def classify_code(code: str | None, table: str = "") -> str:
    """Etiqueta un código: canónico, legado (con su destino), sentinela o desconocido."""
    if code is None:
        return "[DESCONOCIDO — NULL]"
    if code in OPERATIONAL_VERTICALS:
        return "[canónico]"
    if code in LEGACY_TO_CANONICAL:
        return f"[legado → {LEGACY_TO_CANONICAL[code]}]"
    if code in SENTINELAS_POR_TABLA.get(table, frozenset()):
        return "[sentinela histórico — no se migra]"
    return "[DESCONOCIDO]"


async def histogram(session: AsyncSession, table: str, column: str) -> list[tuple[str | None, int]]:
    """Histograma ``(código, filas)`` de una columna de vertical, desc por filas."""
    rows = await session.execute(
        text(
            f"SELECT {column} AS code, count(*) AS total FROM {table} "  # noqa: S608
            f"GROUP BY {column} ORDER BY total DESC, {column}"
        )
    )
    return [(row.code, int(row.total)) for row in rows]


async def count_tenants_without_profile(session: AsyncSession) -> int:
    """Tenants sin ``BusinessProfile`` (el agujero de los signups viejos de Google)."""
    result = await session.execute(
        text(
            "SELECT count(*) FROM tenants t "
            "LEFT JOIN business_profiles bp ON bp.tenant_id = t.tenant_id "
            "WHERE bp.tenant_id IS NULL"
        )
    )
    return int(result.scalar_one())


async def list_tenants_without_profile(session: AsyncSession) -> list[tuple[str, str]]:
    """``(tenant_id, display_name)`` de los tenants sin perfil — para repararlos."""
    rows = await session.execute(
        text(
            "SELECT t.tenant_id, t.display_name FROM tenants t "
            "LEFT JOIN business_profiles bp ON bp.tenant_id = t.tenant_id "
            "WHERE bp.tenant_id IS NULL ORDER BY t.created_at"
        )
    )
    return [(str(row.tenant_id), str(row.display_name)) for row in rows]


async def run_report(session: AsyncSession) -> None:
    bloqueantes: list[str] = []
    no_bloqueantes: list[str] = []

    for label, table, column, bloquea in VERTICAL_COLUMNS:
        print(f"\n=== {label} ===")
        filas = await histogram(session, table, column)
        if not filas:
            print("  (sin filas)")
            continue
        for code, total in filas:
            etiqueta = classify_code(code, table)
            if etiqueta.startswith("[DESCONOCIDO"):
                destino = bloqueantes if bloquea else no_bloqueantes
                destino.append(f"{label} = {code!r} ({total} fila/s)")
            print(f"  {str(code):>20}  {total:>8}  {etiqueta}")

    print("\n=== Tenants sin BusinessProfile ===")
    huerfanos = await count_tenants_without_profile(session)
    print(f"  total: {huerfanos}")
    if huerfanos:
        print(
            "\n  Estos tenants NO tienen rubro configurado. Hasta las Tasks 2-3 los\n"
            "  tapaba un fallback silencioso a kiosco; ahora el código parsea estricto.\n"
            "  Reparalos de a uno (el vertical es obligatorio, no se adivina):\n"
            "    scripts/backfill_missing_business_profiles.py --tenant <uuid> "
            "--vertical <code> --apply\n"
        )
        for tenant_id, display_name in await list_tenants_without_profile(session):
            print(f"    tenant_id={tenant_id}  display_name={display_name!r}")

    print("\n=== Veredicto ===")
    if bloqueantes:
        print(
            "  BLOQUEANTE: business_profiles tiene códigos DESCONOCIDOS. La migración\n"
            "  20260806_0002 va a ABORTAR el deploy en _verify_verticals_canonical\n"
            "  (fail-safe: la versión vieja sigue sirviendo). Decidí a mano a qué rubro\n"
            "  corresponden y corregí esas filas antes de deployar:"
        )
        for item in bloqueantes:
            print(f"    - {item}")
    else:
        print("  La migración 20260806_0002 aplicaría limpio (business_profiles sin sorpresas).")

    if no_bloqueantes:
        print(
            "\n  NO bloquea el deploy, pero conviene revisarlo: códigos desconocidos\n"
            "  fuera de business_profiles. La migración no los adivina (no-invention);\n"
            "  degradan en silencio (muestras de compute_margin_benchmark, catálogo de\n"
            "  campos por rubro). Decidí a mano qué hacer con cada uno:"
        )
        for item in no_bloqueantes:
            print(f"    - {item}")


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
