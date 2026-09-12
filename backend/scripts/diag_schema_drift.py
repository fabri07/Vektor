"""Diagnóstico read-only: ¿el esquema coincide con lo que dice ``alembic_version``?

Usage:
    cd backend && .venv/bin/python scripts/diag_schema_drift.py

Sin argumentos y sin exportar nada: si ``DATABASE_URL`` no está en el entorno, lo
lee de ``backend/.env``. Ese fallback existe porque el `.env` **no se puede
`source`** (la URL de Neon trae un `&` sin comillar y zsh lo lee como operador de
fondo), y la alternativa —una línea con `$(grep ... | cut ...)` adentro de
comillas dobles— se corta al pegarla y deja el shell colgado en `dquote
cmdsubst>`. Un script que lee el archivo no tiene ese problema.

**Sólo SELECT. Nunca escribe. Nunca imprime la URL de conexión** (mismo contrato
que `diag_account.py`): del destino informa host y base, que es lo que hace falta
para saber contra qué se está mirando.

Para qué
--------
``alembic_version`` dice qué migraciones alembic CREE aplicadas. Este script mira
qué objetos existen DE VERDAD. Cuando las dos cosas no coinciden, el deploy falla
con `DuplicateColumn` y el mensaje no alcanza para saber si la deriva es una
columna suelta o media cadena.

Lo que hace falta para decidir un arreglo es el mapa completo: cuáles de los
objetos que la cadena todavía no aplicó ya están, y —el control— si los de la
versión declarada están de verdad. Un control ausente significa que el problema
es más viejo que la migración que falló.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import sys
from urllib.parse import urlparse

import asyncpg

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from _db import normalize_dsn  # noqa: E402

#: Qué mirar, por migración. `("col", tabla, columna)` o `("tabla", nombre)`.
#: El primero es el CONTROL: pertenece a la versión que `alembic_version` declara
#: aplicada, así que tiene que existir. Si no está, la deriva es anterior.
OBJETOS: list[tuple[str, str, tuple[str, ...]]] = [
    ("20260903_0001", "", ("col", "products", "internal_sku")),
    ("20260906_0001", "", ("col", "uploaded_files", "parse_attempt_id")),
    ("20260909_0001", "", ("tabla", "job_runs")),
    ("20260910_0001", "", ("tabla", "operation_identities")),
    ("20260910_0001", "", ("tabla", "operation_identity_links")),
    ("20260910_0002", "", ("col", "products", "external_code_key")),
    ("20260910_0002", "", ("col", "customers", "external_code_key")),
    ("20260910_0002", "", ("col", "suppliers", "external_code_key")),
    ("20260910_0003", "", ("col", "customers", "has_user_edits")),
    ("20260910_0003", "", ("col", "suppliers", "has_user_edits")),
    ("20260910_0004", "", ("tabla", "import_attempts")),
    ("20260910_0004", "", ("tabla", "import_outbox")),
    ("20260910_0005", "", ("col", "import_attempts", "capabilities_json")),
]


def _dsn_crudo() -> str | None:
    """`DATABASE_URL` del entorno; si no está, del `.env` del backend."""
    for var in ("DATABASE_URL", "DATABASE_URL_SYNC"):
        if os.environ.get(var):
            return os.environ[var]
    env = pathlib.Path(__file__).resolve().parent.parent / ".env"
    if not env.exists():
        return None
    for linea in env.read_text().splitlines():
        limpia = linea.strip()
        if limpia.startswith("DATABASE_URL="):
            return limpia.split("=", 1)[1].strip().strip("'\"")
    return None


async def _existe(conn: asyncpg.Connection, objeto: tuple[str, ...]) -> bool:
    if objeto[0] == "tabla":
        fila = await conn.fetchval("select to_regclass($1) is not null", f"public.{objeto[1]}")
    else:
        fila = await conn.fetchval(
            "select exists(select 1 from information_schema.columns "
            "where table_schema = 'public' and table_name = $1 and column_name = $2)",
            objeto[1],
            objeto[2],
        )
    return bool(fila)


async def main() -> int:
    crudo = _dsn_crudo()
    if not crudo:
        print("ERROR: no encontré DATABASE_URL ni en el entorno ni en backend/.env")
        return 2

    dsn, ssl_ctx = normalize_dsn(crudo)
    destino = urlparse(dsn)
    print(f"destino : {destino.hostname}{destino.path}")

    conn = await asyncpg.connect(dsn, ssl=ssl_ctx)
    try:
        versiones = [r["version_num"] for r in await conn.fetch("select * from alembic_version")]
        print(f"alembic : {versiones}")
        if len(versiones) > 1:
            print("  ⚠ MÁS DE UNA fila: leer la versión de un lado y escribir el DDL")
            print("    en otro produce DuplicateColumn en loop.")
        print()

        # Lo que cuenta como deriva depende de la versión DECLARADA, no de una
        # lista fija: un objeto de una migración ya aplicada tiene que existir, y
        # uno de una migración pendiente no debería. Las revisiones son
        # `AAAAMMDD_NNNN`, así que el orden lexicográfico es el de la cadena.
        declarada = max(versiones) if versiones else ""
        ancho = max(len(f"{t[1]}.{t[2]}" if t[0] == "col" else t[1]) for _, _, t in OBJETOS)
        derivados: list[str] = []
        faltantes: list[str] = []
        for mig, _nota, objeto in OBJETOS:
            nombre = f"{objeto[1]}.{objeto[2]}" if objeto[0] == "col" else f"tabla {objeto[1]}"
            hay = await _existe(conn, objeto)
            pendiente = mig > declarada
            if hay and pendiente:
                estado, detalle = "SÍ", "← DERIVA (existe sin estar aplicada)"
                derivados.append(f"{mig} → {nombre}")
            elif hay:
                estado, detalle = "SÍ", "aplicada, existe: ok"
            elif pendiente:
                estado, detalle = "no", "pendiente: ok"
            else:
                estado, detalle = "no", "← FALTA (aplicada según alembic)"
                faltantes.append(f"{mig} → {nombre}")
            print(f"  {mig}  {nombre:<{ancho + 6}} {estado:<3} {detalle}")

        print()
        if faltantes:
            print(f"⚠ FALTAN {len(faltantes)} objeto(s) de migraciones que alembic da por")
            print("  aplicadas. La deriva es ANTERIOR a la migración que falló:")
            for f in faltantes:
                print(f"  - {f}")
        if derivados:
            print(f"DERIVA ({len(derivados)}): existen, pero alembic no los da por aplicados.")
            print("Es lo que hace fallar el upgrade con DuplicateColumn:")
            for d in derivados:
                print(f"  - {d}")
        if not derivados and not faltantes:
            print("Sin deriva: el esquema coincide con lo que alembic declara.")

        # Que el ESQUEMA esté al día no prueba que el CÓDIGO nuevo esté corriendo:
        # las migraciones las aplica un contenedor one-off que puede terminar bien
        # y que el servicio igual no arranque.
        #
        # Lo que sigue es un INDICIO, no una prueba, y la diferencia importa:
        # `job_runs` la escribe SOLO el barrido de relecturas, que agenda Beat — y
        # Beat no está desplegado como servicio. Cero filas ahí se explica entero
        # por eso, sin decir nada del código. Lo mismo con las otras dos: las
        # escribe el confirm de un import, así que están vacías mientras nadie
        # importe. **Filas = señal positiva; cero = no dice nada.**
        #
        # El commit desplegado NO se puede leer desde acá: `/health` devuelve una
        # versión hardcodeada. El único lugar donde está es Railway.
        print()
        print("=== ¿hay código nuevo escribiendo? ===")
        for tabla, col_fecha in (
            ("job_runs", "started_at"),
            ("import_attempts", "created_at"),
            ("operation_identities", "created_at"),
        ):
            if not await _existe(conn, ("tabla", tabla)):
                continue
            tiene_col = await _existe(conn, ("col", tabla, col_fecha))
            sql = (
                f"select count(*) n, max({col_fecha})::text ult from {tabla}"
                if tiene_col
                else f"select count(*) n, null::text ult from {tabla}"
            )
            fila = await conn.fetchrow(sql)
            ultimo = fila["ult"] or "—"
            print(f"  {tabla:<22} filas={fila['n']:<6} último={ultimo}")
        print()
        print("  Filas > 0 = hay código nuevo escribiendo. Cero NO prueba lo")
        print("  contrario: job_runs depende de Beat (no desplegado) y las otras")
        print("  dos las escribe un import. El commit desplegado sólo está en")
        print("  Railway — /health devuelve una versión hardcodeada.")
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
