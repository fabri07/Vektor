"""Crea el ``BusinessProfile`` faltante de un tenant huérfano.

Por qué existe
-------------
``scripts/preflight_vertical_codes.py`` reporta cuántos tenants NO tienen
``BusinessProfile``: son, sobre todo, los signups viejos por Google — el alta
social que existió hasta la Task 15 creaba Tenant + User pero no perfil (esa
función ya no existe: hoy entrar con Google abre una solicitud de acceso).
Hasta las Tasks 2-3 esos tenants sobrevivían porque toda la app tenía un
fallback silencioso a kiosco; ahora que ``parse_vertical`` es estricto, cada uno
es un estado roto real (``/fields`` responde 409, el orquestador degrada).

**El vertical es OBLIGATORIO y no se adivina** (no-invention). No hay ninguna
señal confiable en la base para inferir el rubro de un negocio sin perfil:
inventarlo es exactamente el bug que este PR viene a borrar. Hay que
preguntárselo al dueño y pasarlo por ``--vertical``.

El perfil se crea con el MISMO shape que ``AuthService.register``
(``data_mode='M0'``, ``data_confidence='LOW'``, ``heuristic_profile_version='v1'``)
y con ``onboarding_completed=False``: el usuario completa los números
financieros en su próximo login, no se le inventa ninguno.

Usage:
    # Dry-run (default): dice qué crearía y hace rollback.
    DATABASE_URL='postgresql://...' .venv/bin/python \\
        scripts/backfill_missing_business_profiles.py \\
        --tenant <uuid> --vertical kiosco_almacen

    # Aplicar (después de revisar el dry-run):
    ... --tenant <uuid> --vertical kiosco_almacen --apply

Idempotente: si el tenant ya tiene perfil, no toca nada (ni siquiera con
``--apply``) — nunca pisa un vertical ya configurado.

NUNCA imprime la connection URL. Read-only salvo ``--apply``. Correr desde
backend/.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _db import async_engine_config  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

from app.domain.verticals import Vertical, parse_vertical  # noqa: E402
from app.persistence.models.business import BusinessProfile  # noqa: E402


async def fetch_tenant(session: AsyncSession, tenant_id: uuid.UUID) -> str | None:
    """``display_name`` del tenant, o ``None`` si no existe."""
    row = (
        await session.execute(
            text("SELECT display_name FROM tenants WHERE tenant_id = :tid"),
            {"tid": str(tenant_id)},
        )
    ).first()
    return None if row is None else str(row[0])


async def fetch_existing_vertical(session: AsyncSession, tenant_id: uuid.UUID) -> str | None:
    """``vertical_code`` del perfil actual del tenant, o ``None`` si no tiene perfil."""
    row = (
        await session.execute(
            text("SELECT vertical_code FROM business_profiles WHERE tenant_id = :tid"),
            {"tid": str(tenant_id)},
        )
    ).first()
    return None if row is None else str(row[0])


def build_profile(tenant_id: uuid.UUID, vertical: Vertical) -> BusinessProfile:
    """Perfil nuevo con el mismo shape que ``AuthService.register``.

    Se usa el modelo ORM (y no un INSERT a mano) justamente para que los valores
    por defecto no puedan divergir del alta normal.
    """
    return BusinessProfile(
        tenant_id=tenant_id,
        vertical_code=vertical.value,
        data_mode="M0",
        data_confidence="LOW",
        onboarding_completed=False,
        heuristic_profile_version="v1",
    )


async def run(session: AsyncSession, tenant_id: uuid.UUID, vertical: Vertical, apply: bool) -> int:
    """Devuelve el exit code: 0 ok / nada que hacer, 1 tenant inexistente."""
    modo = "APPLY" if apply else "DRY-RUN"
    print(f"[{modo}] BusinessProfile faltante — tenant_id={tenant_id}")

    display_name = await fetch_tenant(session, tenant_id)
    if display_name is None:
        print(f"  ERROR: no existe ningún tenant con tenant_id={tenant_id}. Nada que hacer.")
        return 1
    print(f"  tenant: {display_name!r}")

    existente = await fetch_existing_vertical(session, tenant_id)
    if existente is not None:
        print(
            f"  SKIP: el tenant YA tiene BusinessProfile (vertical_code={existente!r}). "
            "Este script nunca pisa un vertical ya configurado."
        )
        return 0

    print(
        f"  A CREAR: BusinessProfile(vertical_code={vertical.value!r}, data_mode='M0', "
        "data_confidence='LOW', onboarding_completed=False)"
    )

    if not apply:
        print("\nDry-run: nada se escribió. Volvé a correr con --apply para crearlo.")
        return 0

    session.add(build_profile(tenant_id, vertical))
    await session.commit()
    print(f"\nCOMMIT: BusinessProfile creado con vertical_code={vertical.value!r}.")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Crea el BusinessProfile faltante de un tenant. El vertical es "
            "obligatorio: NO se adivina."
        )
    )
    parser.add_argument("--tenant", required=True, help="tenant_id (uuid) del tenant huérfano")
    parser.add_argument(
        "--vertical",
        required=True,
        choices=[v.value for v in Vertical],
        help="rubro del negocio, confirmado con el dueño (no se infiere)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="escribe de verdad (sin esta flag es un dry-run que hace rollback)",
    )
    return parser.parse_args(argv)


async def main() -> None:
    args = parse_args()
    try:
        tenant_id = uuid.UUID(args.tenant)
    except ValueError:
        print(f"ERROR: --tenant no es un uuid válido: {args.tenant!r}")
        sys.exit(2)
    # `choices` de argparse ya lo restringe; `parse_vertical` es la validación
    # canónica (una sola fuente de verdad, y falla ruidoso si alguien la evade).
    vertical = parse_vertical(args.vertical)

    url, connect_args = async_engine_config()
    engine = create_async_engine(url, connect_args=connect_args)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            codigo = await run(session, tenant_id, vertical, apply=args.apply)
            if not args.apply:
                await session.rollback()
    finally:
        await engine.dispose()
    if codigo:
        sys.exit(codigo)


if __name__ == "__main__":
    asyncio.run(main())
