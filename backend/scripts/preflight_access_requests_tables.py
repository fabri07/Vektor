"""Preflight read-only de M-2: ¿hay tabla residual de `access_requests`?

Por qué existe
--------------
``20260806_0001_access_requests`` es idempotente por inspección::

    existing = set(inspector.get_table_names())
    if "access_requests" not in existing:
        op.create_table(...)   # ← CHECKs, índices y el único parcial van ACÁ ADENTRO

Esa guarda protege contra el doble ``alembic upgrade head`` de un mismo deploy
(varios servicios de Railway pueden correrlo en paralelo), pero tiene un modo de
falla silencioso: si la tabla YA existe por cualquier otro motivo —un intento
previo, un ``create_all`` de un script, una restauración parcial— el
``create_table`` entero se saltea y la migración igual **queda marcada como
aplicada** en ``alembic_version``. Resultado: tabla sin sus seis CHECKs, sin sus
índices y sin el único parcial ``uq_access_requests_open_email``, y nadie se
entera hasta que una fila inválida entra a la base o el listado administrativo
tira 500.

Los CHECKs no son decorativos: son la garantía a nivel DB de que ``'otros'`` es
inescribible como vertical asignado y de que una solicitud aprobada no puede
quedarse sin vertical. Si se instalan a medias, la garantía no existe.

Qué imprime
-----------
1. Si existen ``access_requests`` / ``access_request_tokens`` en el schema actual.
2. El ``version_num`` de ``alembic_version`` (para distinguir "todavía no se
   desplegó" de "se desplegó y quedó a medias").
3. Si alguna tabla existe: sus columnas, sus CHECKs, sus índices y su conteo de
   filas — para poder decidir a mano si se dropea o se completa a mano.
4. Un veredicto explícito: LIBRE / BLOQUEADO.

Este script INFORMA, no repara. Si aparece una tabla residual, la decisión (drop
vs completar los CHECKs a mano) es del dueño: puede haber datos reales adentro.

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

#: Las dos tablas que crea 20260806_0001. Ninguna debe existir antes del deploy.
TABLES = ("access_requests", "access_request_tokens")

#: CHECKs que la migración instala DENTRO del create_table. Si la tabla ya
#: existía, ninguno de estos se agrega. Espejo de 20260806_0001 (hardcodeado a
#: propósito: una migración es una foto del pasado).
EXPECTED_CHECKS = (
    "ck_access_requests_status",
    "ck_access_requests_requested_plan",
    "ck_access_requests_requested_vertical",
    "ck_access_requests_vertical_other_text",
    "ck_access_requests_assigned_vertical_code",
    "ck_access_requests_approved_needs_vertical",
)

#: Índices que van en el mismo bloque condicional, por tabla.
#: ``uq_access_requests_open_email`` es el único parcial sobre ``lower(email)``,
#: que solo se crea en PostgreSQL.
EXPECTED_INDEXES: dict[str, tuple[str, ...]] = {
    "access_requests": (
        "ix_access_requests_email",
        "ix_access_requests_status",
        "ix_access_requests_review_queue",
        "uq_access_requests_open_email",
    ),
    "access_request_tokens": ("ix_access_request_tokens_access_request_id",),
}

#: La revisión que este preflight custodia.
GUARDED_REVISION = "20260806_0001"


async def _existing_tables(session: AsyncSession) -> set[str]:
    rows = await session.execute(
        text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema() "
            "AND table_name = ANY(:names)"
        ),
        {"names": list(TABLES)},
    )
    return {r[0] for r in rows}


async def _alembic_heads(session: AsyncSession) -> list[str]:
    exists = await session.execute(
        text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = current_schema() "
            "AND table_name = 'alembic_version'"
        )
    )
    if exists.first() is None:
        return []
    rows = await session.execute(text("SELECT version_num FROM alembic_version"))
    return [r[0] for r in rows]


async def _describe(session: AsyncSession, table: str) -> None:
    """Vuelca estructura y conteo de una tabla que NO debería existir."""
    print(f"\n  ── Estructura de `{table}` ──")

    cols = await session.execute(
        text(
            "SELECT column_name, data_type, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = :t "
            "ORDER BY ordinal_position"
        ),
        {"t": table},
    )
    col_rows = cols.all()
    print(f"  Columnas ({len(col_rows)}):")
    for name, dtype, nullable in col_rows:
        null_mark = "NULL" if nullable == "YES" else "NOT NULL"
        print(f"    - {name}: {dtype} {null_mark}")

    checks = await session.execute(
        text(
            "SELECT c.conname, pg_get_constraintdef(c.oid) "
            "FROM pg_constraint c "
            "JOIN pg_class t ON t.oid = c.conrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "WHERE t.relname = :t AND n.nspname = current_schema() "
            "AND c.contype = 'c' "
            "ORDER BY c.conname"
        ),
        {"t": table},
    )
    check_rows = checks.all()
    present_checks = {r[0] for r in check_rows}
    print(f"  CHECKs ({len(check_rows)}):")
    for name, definition in check_rows:
        print(f"    - {name}: {definition}")
    # Los seis CHECKs viven todos en access_requests; tokens no tiene ninguno.
    faltantes = (
        [c for c in EXPECTED_CHECKS if c not in present_checks]
        if table == "access_requests"
        else []
    )
    if faltantes:
        print("  ⚠️  CHECKs que la migración NO va a poder agregar (guarda saltea):")
        for name in faltantes:
            print(f"    - {name}")

    idx = await session.execute(
        text(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE schemaname = current_schema() AND tablename = :t "
            "ORDER BY indexname"
        ),
        {"t": table},
    )
    idx_rows = idx.all()
    present_idx = {r[0] for r in idx_rows}
    print(f"  Índices ({len(idx_rows)}):")
    for name, definition in idx_rows:
        print(f"    - {name}: {definition}")
    idx_faltantes = [
        i for i in EXPECTED_INDEXES.get(table, ()) if i not in present_idx
    ]
    if idx_faltantes:
        print("  ⚠️  Índices que la migración NO va a poder agregar:")
        for name in idx_faltantes:
            print(f"    - {name}")

    count = await session.execute(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
    n = count.scalar_one()
    print(f"  Filas: {n}")
    if n:
        print("  ⚠️  HAY DATOS. Un DROP los borra — inspeccionar antes de decidir.")


async def run_report(session: AsyncSession) -> None:
    print("=" * 72)
    print("PREFLIGHT M-2 — tablas residuales de access_requests")
    print("=" * 72)

    heads = await _alembic_heads(session)
    if heads:
        print(f"\nalembic_version: {', '.join(heads)}")
        if GUARDED_REVISION in heads:
            print(
                f"  ⚠️  {GUARDED_REVISION} figura como APLICADA. Si las tablas de "
                "abajo no tienen todos sus CHECKs, quedó a medias."
            )
    else:
        print("\nalembic_version: (tabla ausente — base sin migrar)")

    existing = await _existing_tables(session)

    print("\nTablas que crea la migración:")
    for table in TABLES:
        mark = "EXISTE" if table in existing else "no existe"
        print(f"  - {table}: {mark}")

    if not existing:
        print("\n" + "=" * 72)
        print("✅ LIBRE — ninguna de las dos tablas existe.")
        print("   La migración va a crear todo (tabla + 6 CHECKs + 4 índices).")
        print("=" * 72)
        return

    for table in sorted(existing):
        await _describe(session, table)

    print("\n" + "=" * 72)
    print("🛑 BLOQUEADO — hay tabla residual. NO desplegar todavía.")
    print("   La guarda `if not in existing` va a saltear el create_table entero,")
    print("   y la migración va a quedar marcada como aplicada igual.")
    print("   Decidir a mano: dropear (si no hay datos) o completar los CHECKs.")
    print("=" * 72)


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
