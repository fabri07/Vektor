"""Herramienta ADMINISTRATIVA EXCEPCIONAL: deja un tenant sin datos operativos.

NO es el flujo normal de borrado. El flujo normal es por PROCEDENCIA DE ARCHIVO
(``DELETE /ingestion/files/{id}`` → ``file_deletion_service.revert_file_data``),
que revierte solo lo que ese archivo trajo y respeta lo que tiene otra fuente.

Este script existe para el caso que ese flujo NO puede cubrir: datos de imports
tan viejos que no dejaron ledger (``ingestion_version < 3``), donde no hay forma
de saber qué archivo creó qué. Ahí el borrado por procedencia no puede afirmar
nada, y la única salida honesta es vaciar la cuenta a propósito — nunca adivinando.

QUÉ BORRA
    Todo lo operativo del tenant: ventas, gastos, productos, inventario,
    clientes, proveedores, archivos, "Otros", scores, huellas, cierres de caja,
    alias de mapeo aprendidos, trazas de pipeline, etc.

QUÉ CONSERVA (``_PRESERVAR``)
    La cuenta en sí: ``tenants``, ``users`` y sus identidades/credenciales, y
    ``business_profiles`` (rubro y configuración del negocio). El usuario vuelve
    a entrar con su misma contraseña y su mismo rubro, sin datos.

    Los **alias de mapeo aprendidos** (``tenant_column_mappings``) SÍ se borran a
    propósito: son un aprendizaje derivado de los datos que se están eliminando.
    Conservarlos haría que el próximo import repita las mismas decisiones de
    mapeo que llevaron a los datos que se descartan — incluido, en el caso que
    originó este script, un alias de columna de costo apuntando a precio de venta.

SEGURIDAD
    - Dry-run por DEFAULT. ``--apply`` es explícito.
    - ``--confirm-name`` obligatorio con ``--apply``: hay que tipear el
      ``display_name`` exacto del tenant. Un uuid mal copiado no alcanza para
      vaciar la cuenta equivocada.
    - Un solo tenant por corrida. NO existe ``--all``.
    - Se audita ANTES de borrar (con los conteos), en la misma transacción.
    - Todo en UNA transacción: si algo falla, no queda a medias.

    **NO es reversible.** A diferencia de ``purge_deleted_file_data.py`` (soft
    delete auditado), acá los DELETE son reales. Revisá el dry-run.

Usage:
    # Dry-run (no escribe nada)
    DATABASE_URL='postgresql://...' .venv/bin/python \
        scripts/reset_tenant_data.py --tenant <uuid>

    # Aplicar, tipeando el nombre exacto del negocio
    ... scripts/reset_tenant_data.py --tenant <uuid> --confirm-name 'Asteria Home-Deco' --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

import asyncpg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _db import normalize_dsn  # noqa: E402

_DECISION_TYPE = "TENANT_DATA_RESET"
_TRIGGERED_BY = "script:reset_tenant_data"

# Tablas con `tenant_id` que NO se tocan: son la cuenta, no sus datos.
_PRESERVAR: frozenset[str] = frozenset(
    {
        "tenants",  # la cuenta
        "users",  # los usuarios y sus credenciales
        "user_auth_identities",  # login federado (Google)
        "business_profiles",  # rubro y configuración del negocio
        # El PLAN de la cuenta, no un dato de negocio. La crea
        # `tenant_provisioning` al dar de alta el tenant y `/auth/me` la lee en
        # cada sesión. Borrarla deja la cuenta sin plan: no rompe el login
        # (el endpoint tolera None) pero es un dato administrativo que un reset
        # de DATOS no tiene por qué tocar. Se aprendió borrándola de más en el
        # reset de Asteria (2026-07-31).
        "subscriptions",
        # La auditoría es insert-only por invariante del proyecto: borrar el
        # registro de decisiones para "limpiar" sería exactamente lo contrario a
        # lo que esa tabla existe para garantizar.
        "decision_audit_log",
    }
)

# Tablas que SÍ se borran pero tienen un costo visible para el usuario: no son
# dato de negocio, pero perderlas obliga a rehacer algo a mano. Se avisan aparte
# para que el reset no las borre "en silencio".
_AVISAR_SI_SE_BORRAN: dict[str, str] = {
    "google_mcp_connections": "hay que volver a conectar Google desde /apps",
    "google_oauth_tokens": "hay que volver a conectar Google desde /apps",
    "tenant_column_mappings": "se pierden los alias de columnas aprendidos (a propósito)",
}


def _titulo(texto: str) -> None:
    print(f"\n{'=' * 74}\n  {texto}\n{'=' * 74}")


async def _tablas_con_tenant(conn: asyncpg.Connection) -> list[str]:
    """Tablas del schema public que tienen columna ``tenant_id``.

    Se descubre en vivo en vez de hardcodear una lista: una tabla nueva agregada
    por una migración futura queda cubierta sola. Lo contrario —una lista fija
    que envejece— dejaría datos del tenant vivos sin que nadie se entere.
    """
    filas = await conn.fetch(
        "SELECT table_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND column_name = 'tenant_id' "
        "ORDER BY table_name"
    )
    return [f["table_name"] for f in filas if f["table_name"] not in _PRESERVAR]


async def _contar(conn: asyncpg.Connection, tabla: str, tid: str) -> int:
    valor = await conn.fetchval(
        f'SELECT count(*) FROM "{tabla}" WHERE tenant_id = $1::uuid',  # noqa: S608
        tid,
    )
    return int(valor or 0)


async def _borrar_en_orden(
    conn: asyncpg.Connection, tablas: list[str], tid: str
) -> dict[str, int]:
    """Borra respetando las FKs, sin hardcodear el orden de dependencias.

    Se intenta borrar cada tabla; las que fallan por clave foránea quedan para la
    vuelta siguiente. Se repite mientras haya PROGRESO. Un orden fijo escrito a
    mano se rompe con la primera FK que agregue una migración; esto se acomoda
    solo. Si en una vuelta no se borra ninguna, se corta y se informa — quedarse
    en un bucle infinito sería peor que fallar.
    """
    pendientes = list(tablas)
    borrados: dict[str, int] = {}
    while pendientes:
        progreso = False
        quedan: list[str] = []
        for tabla in pendientes:
            try:
                async with conn.transaction():  # savepoint: aísla el fallo por FK
                    res = await conn.execute(
                        f'DELETE FROM "{tabla}" WHERE tenant_id = $1::uuid',  # noqa: S608
                        tid,
                    )
            except asyncpg.ForeignKeyViolationError:
                quedan.append(tabla)
                continue
            # "DELETE 123" → 123
            cantidad = int(res.split()[-1]) if res.startswith("DELETE") else 0
            if cantidad:
                borrados[tabla] = cantidad
            progreso = True
        if not progreso:
            raise RuntimeError(
                "No se pudo resolver el orden de borrado; quedaron con FK sin "
                f"satisfacer: {', '.join(quedan)}"
            )
        pendientes = quedan
    return borrados


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", required=True, help="tenant_id a vaciar")
    parser.add_argument(
        "--confirm-name",
        help="display_name EXACTO del tenant. Obligatorio con --apply.",
    )
    parser.add_argument("--apply", action="store_true", help="ejecuta (default: dry-run)")
    args = parser.parse_args()

    raw = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_SYNC")
    if not raw:
        print("ERROR: exportá DATABASE_URL antes de correr.")
        return 2
    dsn, sslarg = normalize_dsn(raw)
    conn = await asyncpg.connect(dsn, ssl=sslarg)
    try:
        tenant = await conn.fetchrow(
            "SELECT tenant_id, display_name, status, is_demo FROM tenants "
            "WHERE tenant_id = $1::uuid",
            args.tenant,
        )
        if tenant is None:
            print("No existe ese tenant.")
            return 1

        _titulo("CUENTA A VACIAR")
        print(f"  tenant_id : {tenant['tenant_id']}")
        print(f"  negocio   : {tenant['display_name']}")
        print(f"  status    : {tenant['status']}   is_demo: {tenant['is_demo']}")

        tablas = await _tablas_con_tenant(conn)
        conteos = {t: await _contar(conn, t, args.tenant) for t in tablas}
        con_datos = {t: n for t, n in conteos.items() if n > 0}

        _titulo("SE VA A BORRAR")
        if not con_datos:
            print("  (la cuenta ya está vacía)")
        for tabla, n in sorted(con_datos.items(), key=lambda kv: -kv[1]):
            print(f"  {tabla:<34} {n:>8}")
        print(f"\n  TOTAL de filas: {sum(con_datos.values())}")

        _avisos = {t: m for t, m in _AVISAR_SI_SE_BORRAN.items() if con_datos.get(t)}
        if _avisos:
            _titulo("SE BORRA, Y TIENE COSTO PARA EL USUARIO")
            for tabla, mensaje in sorted(_avisos.items()):
                print(f"  {tabla:<28} → {mensaje}")

        _titulo("SE CONSERVA")
        for tabla in sorted(_PRESERVAR):
            print(f"  {tabla}")

        if not args.apply:
            print("\n  DRY-RUN: no se escribió nada.")
            print("  Para aplicar: --confirm-name '<nombre exacto>' --apply")
            return 0

        # Guard anti-accidente: el uuid se copia y se pega mal; el nombre hay que
        # mirarlo y tipearlo.
        if args.confirm_name != tenant["display_name"]:
            print(
                f"\n  ABORTADO: --confirm-name no coincide.\n"
                f"  Esperado: {tenant['display_name']!r}\n"
                f"  Recibido: {args.confirm_name!r}"
            )
            return 3

        async with conn.transaction():
            # La auditoría va ANTES del borrado y en la MISMA transacción: si el
            # borrado falla, tampoco queda el registro de un reset que no ocurrió.
            await conn.execute(
                "INSERT INTO decision_audit_log "
                "(id, tenant_id, decision_type, decision_data, triggered_by, created_at) "
                "VALUES (gen_random_uuid(), $1::uuid, $2, $3::jsonb, $4, now())",
                args.tenant,
                _DECISION_TYPE,
                json.dumps(
                    {
                        "display_name": tenant["display_name"],
                        "filas_por_tabla": con_datos,
                        "total_filas": sum(con_datos.values()),
                        "preservadas": sorted(_PRESERVAR),
                        "motivo": (
                            "reset administrativo: datos de imports sin ledger, "
                            "no atribuibles a un archivo"
                        ),
                    },
                    default=str,
                ),
                _TRIGGERED_BY,
            )
            borrados = await _borrar_en_orden(conn, tablas, args.tenant)

        _titulo("APLICADO")
        for tabla, n in sorted(borrados.items(), key=lambda kv: -kv[1]):
            print(f"  {tabla:<34} {n:>8}")
        print(f"\n  TOTAL borrado: {sum(borrados.values())} filas")
        print("  La cuenta, sus usuarios y su configuración quedaron intactos.")
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
