"""Vacía los datos operativos de TODOS los tenants. Excepcional y NO reversible.

    cd backend && .venv/bin/python scripts/reset_all_tenants_data.py            # dry-run
    cd backend && .venv/bin/python scripts/reset_all_tenants_data.py \
        --apply --confirmar 'BORRAR TODO'

Qué hace y qué NO
-----------------
No borra nada por su cuenta: **invoca ``reset_tenant_data.py`` una vez por
tenant**. Toda la lógica —qué tablas, en qué orden, la auditoría previa, la
transacción única— vive allá y no se duplica acá. Si esa política cambia, este
runner hereda el cambio.

Conserva la cuenta: ``tenants``, ``users`` con sus credenciales e identidades
federadas, y ``business_profiles``. Cada usuario vuelve a entrar con su misma
contraseña y su mismo rubro, sin datos.

Sobre el guard que este script relaja, dicho de frente
------------------------------------------------------
``reset_tenant_data.py`` exige ``--confirm-name`` con el nombre exacto del
negocio y **no tiene ``--all`` a propósito**: el guard existe para que un uuid mal
copiado no vacíe la cuenta equivocada. Acá el nombre lo lee la base para el mismo
``tenant_id`` que va a vaciar, así que la protección contra "me equivoqué de
cuenta" se mantiene — lo que se pierde es la confirmación humana **una por una**,
que es inherente a pedir "todas". Por eso el token de ``--confirmar`` es literal y
el dry-run es el default: la última revisión es la lista que imprime.

**No es reversible.** Los DELETE son reales (no soft delete). La única vuelta
atrás es restaurar la base a un punto anterior.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import os
import pathlib
import subprocess
import sys

import asyncpg

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from _db import normalize_dsn  # noqa: E402

#: Texto exacto que hay que tipear para ejecutar. No es una formalidad: es el
#: único punto donde alguien afirma, con todas las letras, que quiere esto.
_TOKEN = "BORRAR TODO"

_AQUI = pathlib.Path(__file__).resolve().parent


def _dsn_crudo() -> str | None:
    """`DATABASE_URL` del entorno; si no está, del `.env` del backend."""
    for var in ("DATABASE_URL", "DATABASE_URL_SYNC"):
        if os.environ.get(var):
            return os.environ[var]
    env = _AQUI.parent / ".env"
    if not env.exists():
        return None
    for linea in env.read_text().splitlines():
        if linea.strip().startswith("DATABASE_URL="):
            return linea.strip().split("=", 1)[1].strip().strip("'\"")
    return None


#: Lo que se cuenta para mostrar el tamaño de cada cuenta antes de borrarla. No es
#: la lista de lo que se borra —esa la decide `reset_tenant_data`— sino lo que
#: hace reconocible una cuenta: si estos números son cero, no hay nada que perder.
_TABLAS_VISIBLES = (
    "sales_entries",
    "expense_entries",
    "products",
    "customers",
    "suppliers",
    "uploaded_files",
    "inventory_movements",
)


@dataclasses.dataclass(frozen=True)
class Cuenta:
    tenant_id: str
    nombre: str
    status: str
    conteos: dict[str, int]

    @property
    def total(self) -> int:
        return sum(self.conteos.values())


async def _inventario(conn: asyncpg.Connection) -> list[Cuenta]:
    tenants = await conn.fetch(
        "select tenant_id, display_name, status from tenants order by display_name"
    )
    filas: list[Cuenta] = []
    for t in tenants:
        conteos: dict[str, int] = {}
        for tabla in _TABLAS_VISIBLES:
            existe = await conn.fetchval("select to_regclass($1)", f"public.{tabla}")
            if not existe:
                continue
            conteos[tabla] = await conn.fetchval(
                f"select count(*) from {tabla} where tenant_id = $1", t["tenant_id"]
            )
        filas.append(
            Cuenta(
                tenant_id=str(t["tenant_id"]),
                nombre=str(t["display_name"]),
                status=str(t["status"]),
                conteos=conteos,
            )
        )
    return filas


def _vaciar(tenant_id: str, nombre: str, dsn_original: str) -> int:
    """Invoca `reset_tenant_data.py --apply` para UN tenant."""
    proc = subprocess.run(
        [
            sys.executable,
            str(_AQUI / "reset_tenant_data.py"),
            "--tenant",
            tenant_id,
            "--confirm-name",
            nombre,
            "--apply",
        ],
        env={**os.environ, "DATABASE_URL": dsn_original},
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        print(f"    FALLÓ: {proc.stderr.strip()[-600:]}")
    return proc.returncode


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="ejecuta (default: dry-run)")
    parser.add_argument("--confirmar", default="", help=f"con --apply, exige '{_TOKEN}'")
    args = parser.parse_args()

    crudo = _dsn_crudo()
    if not crudo:
        print("ERROR: no encontré DATABASE_URL ni en el entorno ni en backend/.env")
        return 2

    dsn, ssl_ctx = normalize_dsn(crudo)
    from urllib.parse import urlparse

    destino = urlparse(dsn)
    print(f"destino : {destino.hostname}{destino.path}")
    print(f"modo    : {'APLICAR (destructivo)' if args.apply else 'dry-run (no escribe)'}")
    print()

    conn = await asyncpg.connect(dsn, ssl=ssl_ctx)
    try:
        filas = await _inventario(conn)
    finally:
        await conn.close()

    if not filas:
        print("No hay tenants.")
        return 0

    ancho = max(len(f.nombre) for f in filas)
    total_global = 0
    for f in filas:
        detalle = "  ".join(f"{k.split('_')[0]}={v}" for k, v in f.conteos.items() if v)
        print(f"  {f.nombre:<{ancho}}  {f.status:<10} total={f.total:<7} {detalle}")
        total_global += f.total
    print()
    print(f"{len(filas)} cuenta(s), {total_global} fila(s) de datos operativos en total.")

    if not args.apply:
        print()
        print("Dry-run: no se escribió nada. Para ejecutar:")
        print("  .venv/bin/python scripts/reset_all_tenants_data.py \\")
        print(f"      --apply --confirmar '{_TOKEN}'")
        return 0

    if args.confirmar != _TOKEN:
        print()
        print(f"ABORTADO: --apply exige --confirmar '{_TOKEN}' (recibí {args.confirmar!r}).")
        return 2

    print()
    print("Vaciando…")
    fallas = 0
    for f in filas:
        print(f"  {f.nombre} ({f.total} filas)")
        fallas += 1 if _vaciar(f.tenant_id, f.nombre, crudo) else 0
    print()
    if fallas:
        print(f"TERMINÓ CON {fallas} FALLA(S). Revisá el detalle de arriba.")
        return 1
    print(f"Listo: {len(filas)} cuenta(s) vaciadas. Las cuentas y usuarios quedan intactos.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
