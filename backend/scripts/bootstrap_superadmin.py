"""Crea (o asciende) el usuario ``SUPERADMIN`` de la plataforma.

Por qué existe
-------------
Con el registro abierto cerrado, ``POST /auth/register`` responde **410** y la
única puerta de entrada es la solicitud de acceso, que un ``SUPERADMIN`` tiene
que aprobar. Sobre una base recién reseteada eso es un círculo cerrado: no hay
ningún ``SUPERADMIN``, no se puede crear uno por la app, y todos los endpoints
``/admin/*`` quedan inalcanzables **de manera permanente**.

Mandarse una solicitud a uno mismo y aprobarla con ``scripts/access_requests.py``
tampoco alcanza: eso acuña un **OWNER** de un tenant nuevo, no un ``SUPERADMIN``.

Este script es el único camino de recuperación del sistema nuevo. Se corre una
vez, contra la base vacía, antes de tocar la app.

Los dos modos
-------------
1. **Alta** — acuña la cuenta completa (Tenant + User + Subscription +
   BusinessProfile + MomentumProfile) reusando ``provision_tenant()`` —el mismo
   código que usa la aprobación de una solicitud— y después sube el ``User`` a
   ``role_code='SUPERADMIN'``.
2. **Promoción** (``--promote <email>``) — sube a ``SUPERADMIN`` una cuenta que
   ya existe. Sirve si el dueño ya entró por el flujo de solicitud y prefiere
   ascender esa cuenta en vez de crear otra. Una cuenta ``is_active=False`` NO
   se asciende: el login corta por ``is_active`` antes de mirar el rol, así que
   la promoción no sacaría a nadie del bloqueo.

La contraseña NO se pide por CLI
--------------------------------
Quedaría en el historial del shell y en la salida de ``ps``. El alta crea el
usuario con ``password_hash=None`` (``provision_tenant`` lo resuelve como el
hash de un UUID aleatorio, que nadie conoce) y emite un ``PasswordResetToken``
con el TTL largo de invitación (``_INVITE_TOKEN_TTL_HOURS``, 72 h) — la MISMA
mecánica con la que la aprobación de una solicitud manda su link. El script
imprime **solo la URL** de ``/definir-password?token=...``; el dueño la abre y
define su contraseña ahí.

La promoción NO emite ningún token: esa cuenta ya tiene contraseña y emitir uno
invalidaría los tokens de reset vigentes sin necesidad.

Usage:
    # Dry-run (default): dice exactamente qué crearía y hace rollback.
    DATABASE_URL='postgresql://...' .venv/bin/python \\
        scripts/bootstrap_superadmin.py \\
        --email dueño@vektor.app --full-name "Nombre Apellido" \\
        --business-name "Véktor" --vertical kiosco_almacen

    # Aplicar (después de revisar el dry-run):
    ... --apply

    # Ascender una cuenta existente:
    ... scripts/bootstrap_superadmin.py --promote dueño@vektor.app [--apply]

Idempotente: si el email ya tiene cuenta, el alta FALLA con un mensaje que
sugiere ``--promote`` en vez de duplicar; ``--promote`` sobre un email que no
existe falla sin tocar nada, y sobre una cuenta que YA es ``SUPERADMIN`` no
escribe.

NUNCA imprime la connection URL. Read-only salvo ``--apply``. Correr desde
backend/.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from datetime import UTC, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _db import async_engine_config  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

from app.application.services.auth_service import (  # noqa: E402
    _INVITE_TOKEN_TTL_HOURS,
    AuthService,
)
from app.application.services.tenant_provisioning import provision_tenant  # noqa: E402
from app.config.settings import get_settings  # noqa: E402
from app.domain.verticals import Vertical, parse_vertical  # noqa: E402
from app.persistence.models.audit import DecisionAuditLog  # noqa: E402
from app.persistence.models.tenant import Tenant  # noqa: E402
from app.persistence.models.user import User  # noqa: E402

#: El rol que hace alcanzables los endpoints ``/admin/*`` (``require_role("SUPERADMIN")``).
ROL_SUPERADMIN = "SUPERADMIN"

#: ``decision_type`` de las dos decisiones que audita este script. Crear o
#: ascender un ``SUPERADMIN`` es la escritura de mayor privilegio del sistema:
#: tiene que quedar en el log insert-only aunque la haya hecho una consola.
DECISION_BOOTSTRAP = "SUPERADMIN_BOOTSTRAP"
DECISION_PROMOTED = "SUPERADMIN_PROMOTED"

TRIGGERED_BY = "script:bootstrap_superadmin"


# ── Consultas ────────────────────────────────────────────────────────────────


async def buscar_usuarios_por_email(session: AsyncSession, email: str) -> list[User]:
    """TODOS los usuarios con ese email, mirando todos los tenants.

    No se reusa ``UserRepository.get_by_email_any_tenant`` porque ese hace
    ``limit(1)``: responde "¿existe?", que es lo que necesita el alta pero no la
    promoción. ``users.email`` NO es único global (el único unique es
    ``uq_users_tenant_email``), así que ascender "el primero que aparezca"
    cuando hay dos sería elegir a ciegas a quién le damos el privilegio máximo.
    Con la lista completa el script puede fallar ruidoso en vez de adivinar.
    """
    filas = await session.execute(
        select(User).where(User.email == email.lower()).order_by(User.created_at)
    )
    return list(filas.scalars().all())


async def nombre_de_tenant(session: AsyncSession, tenant_id: uuid.UUID) -> str:
    """``display_name`` del tenant — solo para que la salida sea legible."""
    fila = (
        await session.execute(select(Tenant.display_name).where(Tenant.tenant_id == tenant_id))
    ).first()
    return "(tenant inexistente)" if fila is None else str(fila[0])


# ── Auditoría ────────────────────────────────────────────────────────────────


def auditar(
    session: AsyncSession,
    *,
    decision_type: str,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    email: str,
    datos: dict[str, object],
) -> None:
    """Agrega la traza a ``decision_audit_log`` DENTRO de la transacción.

    ``add`` y NO ``commit``: la auditoría viaja con la decisión que audita, para
    que no exista el caso "se creó el SUPERADMIN y la traza no".
    """
    session.add(
        DecisionAuditLog(
            tenant_id=tenant_id,
            decision_type=decision_type,
            decision_data={
                "user_id": str(user_id),
                "email": email,
                "role_code": ROL_SUPERADMIN,
                **datos,
            },
            triggered_by=TRIGGERED_BY,
            actor_user_id=None,
            context={"endpoint": "bootstrap_superadmin"},
            created_at=datetime.now(UTC),
        )
    )


def url_de_invitacion(token_id: uuid.UUID) -> str:
    """Link para definir la contraseña. Mismo formato que el mail de aprobación."""
    return f"{get_settings().FRONTEND_URL}/definir-password?token={token_id}"


# ── Alta ─────────────────────────────────────────────────────────────────────


async def cmd_alta(session: AsyncSession, args: argparse.Namespace) -> int:
    """Acuña la cuenta y la deja en ``SUPERADMIN``. 0 ok, 1 el email ya existe."""
    email = args.email.strip().lower()
    # `choices` de argparse ya restringe el valor; `parse_vertical` es la
    # validación canónica y falla ruidoso si alguien la evade.
    vertical = parse_vertical(args.vertical)
    modo = "APPLY" if args.apply else "DRY-RUN"

    print(f"\n[{modo}] Alta del SUPERADMIN\n")

    existentes = await buscar_usuarios_por_email(session, email)
    if existentes:
        print(
            f"  ERROR: el email {email} YA tiene cuenta "
            f"(role_code={existentes[0].role_code!r}). No se crea una segunda.\n"
            f"  Si querés ascender esa cuenta, corré:\n"
            f"    scripts/bootstrap_superadmin.py --promote {email} --apply"
        )
        return 1

    print(f"  {'Tenant a crear:':<24}{args.business_name!r} (status ACTIVE, moneda ARS)")
    print(f"  {'Usuario a crear:':<24}{email}  ({args.full_name!r})")
    print(f"  {'role_code final:':<24}{ROL_SUPERADMIN}")
    print(f"  {'':<24}(provision_tenant lo crea OWNER; el script lo sube a SUPERADMIN)")
    print(f"  {'Suscripción:':<24}FREE")
    print(f"  {'BusinessProfile:':<24}vertical_code={vertical.value!r}, onboarding_completed=False")
    print(f"  {'MomentumProfile:':<24}vacío")
    print(f"  {'Contraseña:':<24}NO se pide por CLI (quedaría en el historial del shell).")
    print(
        f"  {'':<24}Se emite un link de invitación de "
        f"{_INVITE_TOKEN_TTL_HOURS} h para definirla."
    )

    if not args.apply:
        print("\nDry-run: no se escribió nada. Repetí con --apply para crear la cuenta.")
        return 0

    tenant, user = await provision_tenant(
        session,
        business_name=args.business_name,
        email=email,
        full_name=args.full_name,
        phone=None,
        vertical=vertical,
        password_hash=None,
        is_active=True,
    )
    # provision_tenant crea el User como OWNER (es el alta normal de un negocio).
    # El privilegio de plataforma se agrega acá, explícito y en la misma
    # transacción, para no duplicar el acuñado de las 5 filas.
    user.role_code = ROL_SUPERADMIN
    await session.flush()

    invitacion = await AuthService(session)._create_password_reset_token(
        user.user_id, ttl_hours=_INVITE_TOKEN_TTL_HOURS
    )
    auditar(
        session,
        decision_type=DECISION_BOOTSTRAP,
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        email=email,
        datos={
            "tenant_id": str(tenant.tenant_id),
            "business_name": args.business_name,
            "vertical_code": vertical.value,
            "invite_ttl_hours": _INVITE_TOKEN_TTL_HOURS,
        },
    )
    await session.commit()

    print(
        f"\nCOMMIT: SUPERADMIN creado.\n"
        f"  tenant_id: {tenant.tenant_id}\n"
        f"  user_id:   {user.user_id}\n"
        f"  role_code: {user.role_code}\n"
        f"\nAbrí este link para definir tu contraseña (vale "
        f"{_INVITE_TOKEN_TTL_HOURS} horas, un solo uso):\n"
        f"  {url_de_invitacion(invitacion.token_id)}\n"
        f"\nDespués entrá por /login con {email} y la contraseña que definas."
    )
    return 0


# ── Promoción ────────────────────────────────────────────────────────────────


async def cmd_promote(session: AsyncSession, args: argparse.Namespace) -> int:
    """Sube a ``SUPERADMIN`` una cuenta existente.

    Devuelve 0 si ascendió (o si ya era ``SUPERADMIN``), y 1 si el email no
    existe, es ambiguo entre tenants, o la cuenta está inactiva.
    """
    email = args.promote.strip().lower()
    modo = "APPLY" if args.apply else "DRY-RUN"

    print(f"\n[{modo}] Promoción a SUPERADMIN\n")

    candidatos = await buscar_usuarios_por_email(session, email)
    if not candidatos:
        print(
            f"  ERROR: no existe ningún usuario con el email {email}. Nada que hacer.\n"
            "  Si todavía no tenés cuenta, creala con el modo de alta:\n"
            "    scripts/bootstrap_superadmin.py --email <mail> --full-name '<nombre>' "
            "--business-name '<negocio>' --vertical <code> --apply"
        )
        return 1
    if len(candidatos) > 1:
        # users.email no es único global: promover "el primero" sería elegir a
        # ciegas a quién le damos el privilegio máximo.
        print(
            f"  ERROR: hay {len(candidatos)} usuarios con el email {email}, en tenants distintos:"
        )
        for candidato in candidatos:
            print(
                f"    user_id={candidato.user_id}  tenant_id={candidato.tenant_id}  "
                f"role_code={candidato.role_code}"
            )
        print("  Resolvelo a mano: este script no elige por vos a quién ascender.")
        return 1

    user = candidatos[0]
    display_name = await nombre_de_tenant(session, user.tenant_id)
    print(f"  {'Usuario:':<24}{email}  ({user.full_name!r})")
    print(f"  {'user_id:':<24}{user.user_id}")
    print(f"  {'Tenant:':<24}{display_name!r} ({user.tenant_id})")
    print(f"  {'Cuenta:':<24}{'activa' if user.is_active else 'INACTIVA'}")
    print(f"  {'role_code actual:':<24}{user.role_code}")
    print(f"  {'role_code nuevo:':<24}{ROL_SUPERADMIN}")
    print(f"  {'Contraseña:':<24}intacta — la promoción no emite ningún link.")

    if not user.is_active:
        # `login` rechaza `is_active=False` antes de mirar el rol: ascender esta
        # cuenta imprimiría "COMMIT" y dejaría al dueño exactamente igual de
        # afuera. Se falla acá, ruidoso, en vez de festejar una promoción que no
        # sirve. Activarla NO es decisión de este script: `is_active=False`
        # significa email sin verificar, y saltearlo desde una consola sería
        # justamente el atajo silencioso que el sistema nuevo cerró.
        print(
            "\n  ERROR: la cuenta está INACTIVA y no se asciende.\n"
            "  Un SUPERADMIN inactivo no puede loguear (el login corta por is_active\n"
            "  antes de mirar el rol), así que la promoción no te sacaría del bloqueo.\n"
            "  Verificá el email de esa cuenta y volvé a correr esto, o creá una cuenta\n"
            "  nueva y activa con el modo de alta."
        )
        return 1

    if user.role_code == ROL_SUPERADMIN:
        print("\nSIN CAMBIOS: la cuenta YA es SUPERADMIN. No se escribió nada.")
        return 0

    if not args.apply:
        print("\nDry-run: no se escribió nada. Repetí con --apply para ascender la cuenta.")
        return 0

    rol_previo = user.role_code
    user.role_code = ROL_SUPERADMIN
    await session.flush()
    auditar(
        session,
        decision_type=DECISION_PROMOTED,
        tenant_id=user.tenant_id,
        user_id=user.user_id,
        email=email,
        datos={"tenant_id": str(user.tenant_id), "role_code_previo": rol_previo},
    )
    await session.commit()

    print(
        f"\nCOMMIT: {email} pasó de {rol_previo} a {ROL_SUPERADMIN}.\n"
        "  Cerrá sesión y volvé a entrar: el role_code viaja en el JWT y el "
        "token viejo sigue diciendo el rol anterior."
    )
    return 0


# ── CLI ──────────────────────────────────────────────────────────────────────


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Crea el SUPERADMIN de la plataforma sobre una base vacía, o asciende "
            "una cuenta existente. Nunca recibe la contraseña por CLI."
        )
    )
    parser.add_argument("--email", help="email del SUPERADMIN a crear")
    parser.add_argument("--full-name", help="nombre y apellido del dueño")
    parser.add_argument("--business-name", help="nombre del negocio del tenant a acuñar")
    parser.add_argument(
        "--vertical",
        choices=[v.value for v in Vertical],
        help="rubro del negocio (no se infiere)",
    )
    parser.add_argument(
        "--promote",
        metavar="EMAIL",
        help="sube a SUPERADMIN una cuenta que YA existe (excluyente con el alta)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="escribe de verdad (sin esta flag es un dry-run que hace rollback)",
    )

    args = parser.parse_args(argv)
    campos_de_alta = ("email", "full_name", "business_name", "vertical")
    usados = [campo for campo in campos_de_alta if getattr(args, campo) is not None]

    if args.promote is not None:
        if usados:
            parser.error(
                "--promote es excluyente con el alta: no lo combines con "
                + ", ".join(f"--{campo.replace('_', '-')}" for campo in usados)
            )
        args.handler = cmd_promote
        return args

    faltantes = [campo for campo in campos_de_alta if getattr(args, campo) is None]
    if faltantes:
        parser.error(
            "el alta exige "
            + ", ".join(f"--{campo.replace('_', '-')}" for campo in campos_de_alta)
            + " (falta: "
            + ", ".join(f"--{campo.replace('_', '-')}" for campo in faltantes)
            + "). Para ascender una cuenta existente usá --promote <email>."
        )
    args.handler = cmd_alta
    return args


async def main() -> None:
    args = parse_args()

    url, connect_args = async_engine_config()
    engine = create_async_engine(url, connect_args=connect_args)
    codigo = 0
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            codigo = await args.handler(session, args)
            if not args.apply:
                # Red de seguridad del dry-run: aunque ningún camino sin --apply
                # escribe, cualquier cosa pendiente en la sesión muere acá.
                await session.rollback()
    finally:
        await engine.dispose()
    if codigo:
        sys.exit(codigo)


if __name__ == "__main__":
    asyncio.run(main())
