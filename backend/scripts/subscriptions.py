"""Consola de suscripciones — activación manual mientras no exista Mercado Pago.

Véktor no tiene todavía una pasarela de pago integrada (Etapa 3 de la política
de planes, `docs/plans/`). Hasta que exista, un cliente que paga por
transferencia o link manual de Mercado Pago se activa DESDE ACÁ — nunca a
mano en la base, para que quede auditado igual que cualquier otra decisión
(`decision_audit_log`, insert-only).

Subcomandos
-----------
::

    show <tenant_id|email>
    activate <tenant_id|email> --plan control --price-usd 27 --price-ars 41500
             --reference "MP-123456" [--months 1] [--notes "..."] [--apply]
    set-status <tenant_id|email> --status grace|read_only|cancelled|active
               [--notes "..."] [--apply]
    expire-due [--apply]
    diagnose                      # solo lectura: qué tenants quedarían bloqueados

Reglas que este archivo sostiene
--------------------------------
1. **``--apply`` es el switch de escritura** (misma convención que
   ``access_requests.py`` y los otros 16 scripts operativos del repo).
2. **``activate`` es idempotente por ``--reference``**: repetir el mismo
   comando con la MISMA referencia de pago no extiende el período ni resetea
   cupos dos veces — busca un `MANUAL_PLAN_ACTIVATION` previo con esa
   referencia para este tenant antes de escribir nada.
3. **Los cupos y el precio quedan copiados a la `Subscription`** (`granted_*`,
   `plan_price_*`) en el momento de activar — nunca resueltos en vivo contra
   `plan_definitions`. Cambiar el catálogo después no altera un período ya
   otorgado (ver `app/domain/subscription.py`).
4. **``expire-due`` persiste transiciones por fecha, pero el enforcement real
   NUNCA depende de que este comando haya corrido** — `effective_access()` ya
   las calcula en caliente en cada request. Esto es solo para que
   `SELECT status FROM subscriptions` refleje la realidad sin tener que
   esperar al próximo intento de uso.
5. Todo cambio de estado/plan pasa por `decision_audit_log` en la MISMA
   transacción — nunca un `UPDATE` suelto.
6. No importa ``app.main``: importa los modelos y servicios directo.

Usage::

    DATABASE_URL='postgresql://...' .venv/bin/python scripts/subscriptions.py show <tenant_id>
    ... scripts/subscriptions.py activate <tenant_id> --plan control \\
        --price-usd 27 --price-ars 41500 --reference "MP-123456" --apply

Todas las fechas se imprimen en **UTC**. Correr desde ``backend/``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _db import async_engine_config  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

from app.application.services.subscription_service import resolve_access  # noqa: E402
from app.domain.subscription import (  # noqa: E402
    GRACE_DAYS,
    LEGACY_FREE_PLAN_CODE,
    AssignablePlan,
    SubscriptionStatus,
)
from app.persistence.models.audit import DecisionAuditLog  # noqa: E402
from app.persistence.models.tenant import (  # noqa: E402
    PlanDefinition,
    Subscription,
    SubscriptionQuotaUsage,
    Tenant,
)
from app.persistence.models.user import User  # noqa: E402
from app.persistence.repositories.tenant_repository import TenantRepository  # noqa: E402

VIA_SCRIPT = "script"

DECISION_ACTIVATION = "MANUAL_PLAN_ACTIVATION"
DECISION_STATUS_CHANGE = "MANUAL_SUBSCRIPTION_STATUS_CHANGE"
DECISION_EXPIRE_DUE = "SUBSCRIPTION_EXPIRE_DUE"


# ── Resolución de tenant ─────────────────────────────────────────────────────


async def resolver_tenant(session: AsyncSession, referencia: str) -> Tenant | None:
    """`tenant_id` (uuid) o email de CUALQUIER usuario de ese tenant."""
    try:
        tenant_id = uuid.UUID(referencia)
    except ValueError:
        resultado = await session.execute(
            select(User).where(User.email == referencia.lower())
        )
        usuario = resultado.scalar_one_or_none()
        if usuario is None:
            return None
        tenant_id = usuario.tenant_id
    return await TenantRepository(session).get_by_id(tenant_id)


def agregar_apply(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--apply",
        action="store_true",
        help="escribe de verdad (sin esta flag es un dry-run que hace rollback)",
    )


def _decimal(valor: str, nombre: str) -> Decimal:
    try:
        return Decimal(valor)
    except InvalidOperation:
        print(f"ERROR: --{nombre} {valor!r} no es un número válido.")
        sys.exit(2)


async def _auditar(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    decision_type: str,
    extra: dict[str, object],
) -> None:
    session.add(
        DecisionAuditLog(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            decision_type=decision_type,
            decision_data=extra,
            triggered_by=f"{VIA_SCRIPT}:subscriptions",
            created_at=datetime.now(UTC),
        )
    )


# ── show ─────────────────────────────────────────────────────────────────────


async def cmd_show(session: AsyncSession, args: argparse.Namespace) -> int:
    tenant = await resolver_tenant(session, args.referencia)
    if tenant is None:
        print(f"No existe ningún tenant con id ni email {args.referencia!r}.")
        return 1

    sub = await TenantRepository(session).get_current_subscription(tenant.tenant_id)
    print(f"\n  {'Tenant:':<24}{tenant.display_name} ({tenant.tenant_id})")
    if sub is None:
        print("  Sin ninguna suscripción viva (todas canceladas, o ninguna todavía).")
        return 0

    print(f"  {'Plan:':<24}{sub.plan_code}")
    print(f"  {'Estado:':<24}{sub.status}")
    print(f"  {'Asientos incluidos:':<24}{sub.seats_included}")
    print(
        f"  {'Cupos otorgados:':<24}"
        f"IA {sub.granted_ia_queries_per_month} · "
        f"Import {sub.granted_imports_per_month} · "
        f"Lecturas {sub.granted_photo_pdf_reads_per_month}"
    )
    precio = f"USD {sub.plan_price_usd_reference} / ARS {sub.plan_price_ars}"
    print(f"  {'Precio otorgado:':<24}{precio}")
    print(f"  {'Prueba vence:':<24}{sub.trial_ends_at or '—'}")
    periodo = f"{sub.current_period_start or '—'} → {sub.current_period_end or '—'}"
    print(f"  {'Período vigente:':<24}{periodo}")
    print(f"  {'Gracia vence:':<24}{sub.grace_ends_at or '—'}")

    usos = (
        await session.execute(
            select(SubscriptionQuotaUsage)
            .where(SubscriptionQuotaUsage.tenant_id == tenant.tenant_id)
            .order_by(SubscriptionQuotaUsage.period_start.desc())
            .limit(6)
        )
    ).scalars().all()
    if usos:
        print("\n  Cupos consumidos (últimos períodos):")
        for uso in usos:
            print(
                f"    {uso.resource:<15} used={uso.used:<4} reserved={uso.reserved:<4} "
                f"limit={uso.limit_snapshot:<7} período {uso.period_start.date()} → "
                f"{uso.period_end.date()}"
            )
    return 0


# ── activate ─────────────────────────────────────────────────────────────────


async def cmd_activate(session: AsyncSession, args: argparse.Namespace) -> int:
    tenant = await resolver_tenant(session, args.referencia)
    if tenant is None:
        print(f"No existe ningún tenant con id ni email {args.referencia!r}.")
        return 1

    sub = await TenantRepository(session).get_current_subscription(tenant.tenant_id)
    if sub is None:
        print(f"El tenant {tenant.tenant_id} no tiene ninguna suscripción viva para activar.")
        return 1

    plan = await session.get(PlanDefinition, args.plan)
    if plan is None:
        print(f"ERROR: no existe el plan {args.plan!r} en plan_definitions.")
        return 1

    precio_usd = _decimal(args.price_usd, "price-usd")
    precio_ars = _decimal(args.price_ars, "price-ars")

    # Idempotencia por --reference: repetir el mismo comando no vuelve a
    # extender el período ni a resetear cupos.
    previa = (
        await session.execute(
            select(DecisionAuditLog)
            .where(
                DecisionAuditLog.tenant_id == tenant.tenant_id,
                DecisionAuditLog.decision_type == DECISION_ACTIVATION,
            )
            .order_by(DecisionAuditLog.created_at.desc())
        )
    ).scalars().all()
    ya_aplicada = next(
        (a for a in previa if a.decision_data.get("reference") == args.reference), None
    )

    modo = "APPLY" if args.apply else "DRY-RUN"
    print(f"\n[{modo}] Activar suscripción de {tenant.display_name}\n")
    print(f"  {'Plan actual:':<22}{sub.plan_code} ({sub.status})")
    print(f"  {'Plan nuevo:':<22}{plan.plan_code}")
    print(f"  {'Precio acordado:':<22}USD {precio_usd} / ARS {precio_ars}")
    print(f"  {'Referencia de pago:':<22}{args.reference}")
    print(
        f"  {'Cupos a otorgar:':<22}IA {plan.ia_queries_per_month} · "
        f"Import {plan.imports_per_month} · Lecturas {plan.photo_pdf_reads_per_month} · "
        f"{plan.seats_included} asientos"
    )

    if ya_aplicada is not None:
        print(
            f"\nSIN CAMBIOS: la referencia {args.reference!r} ya se activó el "
            f"{ya_aplicada.created_at}. No se vuelve a extender el período."
        )
        return 0

    if not args.apply:
        print("\nDry-run: no se escribió nada. Repetí con --apply para activar.")
        return 0

    ahora = datetime.now(UTC)
    fin = ahora + timedelta(days=30 * args.months)

    antes = {
        "plan_code": sub.plan_code,
        "status": sub.status,
        "granted_ia_queries_per_month": sub.granted_ia_queries_per_month,
        "granted_imports_per_month": sub.granted_imports_per_month,
        "granted_photo_pdf_reads_per_month": sub.granted_photo_pdf_reads_per_month,
    }

    sub.plan_code = plan.plan_code
    sub.status = SubscriptionStatus.ACTIVE.value
    sub.seats_included = plan.seats_included
    sub.granted_ia_queries_per_month = plan.ia_queries_per_month
    sub.granted_imports_per_month = plan.imports_per_month
    sub.granted_photo_pdf_reads_per_month = plan.photo_pdf_reads_per_month
    sub.plan_price_usd_reference = precio_usd
    sub.plan_price_ars = precio_ars
    sub.current_period_start = ahora
    sub.current_period_end = fin
    sub.grace_ends_at = None
    sub.cancel_at_period_end = False

    await _auditar(
        session,
        tenant_id=tenant.tenant_id,
        decision_type=DECISION_ACTIVATION,
        extra={
            "reference": args.reference,
            "before": antes,
            "plan_code": plan.plan_code,
            "price_usd": str(precio_usd),
            "price_ars": str(precio_ars),
            "period_start": ahora.isoformat(),
            "period_end": fin.isoformat(),
            "notes": args.notes,
        },
    )
    await session.commit()
    print(f"\nCOMMIT: suscripción activada hasta {fin.date()}.")
    return 0


# ── set-status ───────────────────────────────────────────────────────────────


async def cmd_set_status(session: AsyncSession, args: argparse.Namespace) -> int:
    tenant = await resolver_tenant(session, args.referencia)
    if tenant is None:
        print(f"No existe ningún tenant con id ni email {args.referencia!r}.")
        return 1

    sub = await TenantRepository(session).get_current_subscription(tenant.tenant_id)
    if sub is None:
        print(f"El tenant {tenant.tenant_id} no tiene ninguna suscripción viva.")
        return 1

    nuevo_estado = SubscriptionStatus(args.status)
    print(
        f"\n[{'APPLY' if args.apply else 'DRY-RUN'}] {sub.status} → {nuevo_estado.value} "
        f"para {tenant.display_name}\n"
    )
    if not args.apply:
        print("Dry-run: no se escribió nada. Repetí con --apply para aplicar.")
        return 0

    estado_anterior = sub.status
    sub.status = nuevo_estado.value
    if nuevo_estado == SubscriptionStatus.GRACE and sub.grace_ends_at is None:
        base = sub.current_period_end or datetime.now(UTC)
        sub.grace_ends_at = base + timedelta(days=GRACE_DAYS)

    await _auditar(
        session,
        tenant_id=tenant.tenant_id,
        decision_type=DECISION_STATUS_CHANGE,
        extra={
            "status_before": estado_anterior,
            "status_after": nuevo_estado.value,
            "notes": args.notes,
        },
    )
    await session.commit()
    print(f"COMMIT: estado actualizado a {nuevo_estado.value}.")
    return 0


# ── expire-due ───────────────────────────────────────────────────────────────


async def cmd_expire_due(session: AsyncSession, args: argparse.Namespace) -> int:
    """Persiste (no calcula por primera vez) los vencimientos por fecha.

    `effective_access()` ya bloquea estas suscripciones en caliente aunque
    este comando nunca corra — esto es solo para que la columna `status`
    en la base no quede desactualizada.
    """
    ahora = datetime.now(UTC)
    cambios = 0

    trials_vencidas = (
        await session.execute(
            select(Subscription).where(
                Subscription.status == SubscriptionStatus.TRIAL.value,
                Subscription.trial_ends_at.is_not(None),
                Subscription.trial_ends_at < ahora,
            )
        )
    ).scalars().all()
    activas_vencidas = (
        await session.execute(
            select(Subscription).where(
                Subscription.status == SubscriptionStatus.ACTIVE.value,
                # FREE legado no tiene ciclo comercial (mismo criterio que
                # `effective_access`): sus fechas nunca fueron un vencimiento.
                Subscription.plan_code != LEGACY_FREE_PLAN_CODE,
                Subscription.current_period_end.is_not(None),
                Subscription.current_period_end < ahora,
            )
        )
    ).scalars().all()
    gracias_vencidas = (
        await session.execute(
            select(Subscription).where(
                Subscription.status == SubscriptionStatus.GRACE.value,
                Subscription.grace_ends_at.is_not(None),
                Subscription.grace_ends_at < ahora,
            )
        )
    ).scalars().all()

    print(f"\n[{'APPLY' if args.apply else 'DRY-RUN'}] Vencimientos por fecha\n")
    print(f"  Pruebas vencidas → READ_ONLY: {len(trials_vencidas)}")
    print(f"  Períodos pagos vencidos → GRACE: {len(activas_vencidas)}")
    print(f"  Gracias vencidas → READ_ONLY: {len(gracias_vencidas)}")

    if not args.apply:
        print("\nDry-run: no se escribió nada. Repetí con --apply para aplicar.")
        return 0

    for sub in trials_vencidas:
        sub.status = SubscriptionStatus.READ_ONLY.value
        await _auditar(
            session,
            tenant_id=sub.tenant_id,
            decision_type=DECISION_EXPIRE_DUE,
            extra={
                "status_before": "TRIAL",
                "status_after": "READ_ONLY",
                "reason": "trial_vencida",
            },
        )
        cambios += 1
    for sub in activas_vencidas:
        sub.status = SubscriptionStatus.GRACE.value
        sub.grace_ends_at = (sub.current_period_end or ahora) + timedelta(days=GRACE_DAYS)
        await _auditar(
            session,
            tenant_id=sub.tenant_id,
            decision_type=DECISION_EXPIRE_DUE,
            extra={"status_before": "ACTIVE", "status_after": "GRACE", "reason": "periodo_vencido"},
        )
        cambios += 1
    for sub in gracias_vencidas:
        sub.status = SubscriptionStatus.READ_ONLY.value
        await _auditar(
            session,
            tenant_id=sub.tenant_id,
            decision_type=DECISION_EXPIRE_DUE,
            extra={
                "status_before": "GRACE",
                "status_after": "READ_ONLY",
                "reason": "gracia_vencida",
            },
        )
        cambios += 1

    await session.commit()
    print(f"\nCOMMIT: {cambios} suscripciones actualizadas.")
    return 0


async def cmd_diagnose(session: AsyncSession, args: argparse.Namespace) -> int:
    """Solo lectura: qué tenants quedarían BLOQUEADOS por los controles.

    Correr contra producción ANTES de desplegar un cambio en las reglas de
    acceso. Los controles rechazan a un tenant sin ninguna `Subscription`
    (503) y a un plan pago `ACTIVE` sin período (402): los dos son datos
    rotos, y si existen hay que repararlos antes, no descubrirlos con el
    cliente adentro. Devuelve 1 si encontró alguno.
    """
    sin_suscripcion = (
        await session.execute(
            select(Tenant.tenant_id, Tenant.display_name, Tenant.status)
            .outerjoin(Subscription, Subscription.tenant_id == Tenant.tenant_id)
            .where(Subscription.subscription_id.is_(None))
        )
    ).all()
    pagas_sin_periodo = (
        await session.execute(
            select(Subscription.tenant_id, Subscription.plan_code).where(
                Subscription.status == SubscriptionStatus.ACTIVE.value,
                Subscription.plan_code != LEGACY_FREE_PLAN_CODE,
                Subscription.current_period_end.is_(None),
            )
        )
    ).all()
    por_estado = (
        await session.execute(
            select(Subscription.plan_code, Subscription.status, func.count())
            .group_by(Subscription.plan_code, Subscription.status)
            .order_by(Subscription.plan_code, Subscription.status)
        )
    ).all()

    activos = (
        select(User.tenant_id, func.count().label("n"))
        .where(User.is_active.is_(True))
        .group_by(User.tenant_id)
        .subquery()
    )
    excedidos = [
        (sub, n)
        for sub, n in (
            await session.execute(
                select(Subscription, activos.c.n)
                .join(activos, activos.c.tenant_id == Subscription.tenant_id)
                .where(
                    Subscription.plan_code != LEGACY_FREE_PLAN_CODE,
                    Subscription.status != SubscriptionStatus.CANCELLED.value,
                )
            )
        ).all()
        if n > resolve_access(sub).quota.seats_included
    ]

    print("\nSuscripciones por plan y estado persistido:")
    for plan, estado, cantidad in por_estado:
        print(f"  {plan:<12}{estado:<12}{cantidad}")
    print(f"\nTenants SIN ninguna suscripción (quedarían en 503): {len(sin_suscripcion)}")
    for tenant_id, nombre, estado in sin_suscripcion:
        print(f"  {tenant_id}  {nombre}  [{estado}]")
    print(f"Planes pagos ACTIVE sin período (quedarían en 402): {len(pagas_sin_periodo)}")
    for tenant_id, plan in pagas_sin_periodo:
        print(f"  {tenant_id}  {plan}")
    # Informativo (no cuenta como roto): conservan sus usuarios, solo no suman.
    print(f"Tenants con más usuarios activos que plazas: {len(excedidos)}")
    for sub, n in excedidos:
        print(f"  {sub.tenant_id}  {sub.plan_code}  {n} activos")
    return 1 if sin_suscripcion or pagas_sin_periodo else 0


# ── CLI ──────────────────────────────────────────────────────────────────────


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Consola de suscripciones. Sin --apply, todo comando mutante es un "
            "dry-run que imprime qué haría y hace rollback."
        )
    )
    sub = parser.add_subparsers(dest="comando", required=True)

    p_show = sub.add_parser("show", help="ficha de la suscripción de un tenant")
    p_show.add_argument("referencia", help="tenant_id (uuid) o email de un usuario suyo")
    p_show.set_defaults(handler=cmd_show)

    p_activate = sub.add_parser("activate", help="activar un plan pago (cobro manual)")
    p_activate.add_argument("referencia", help="tenant_id (uuid) o email de un usuario suyo")
    p_activate.add_argument("--plan", required=True, choices=[p.value for p in AssignablePlan])
    p_activate.add_argument("--price-usd", required=True, help="precio acordado en USD")
    p_activate.add_argument("--price-ars", required=True, help="precio acordado en ARS")
    p_activate.add_argument(
        "--reference", required=True, help="referencia del pago verificado (ej. id de MP)"
    )
    p_activate.add_argument(
        "--months", type=int, default=1, help="duración del período (default 1)"
    )
    p_activate.add_argument("--notes", default=None, help="nota interna")
    agregar_apply(p_activate)
    p_activate.set_defaults(handler=cmd_activate)

    p_status = sub.add_parser("set-status", help="cambiar el estado a mano")
    p_status.add_argument("referencia", help="tenant_id (uuid) o email de un usuario suyo")
    p_status.add_argument("--status", required=True, choices=[s.value for s in SubscriptionStatus])
    p_status.add_argument("--notes", default=None, help="motivo del cambio")
    agregar_apply(p_status)
    p_status.set_defaults(handler=cmd_set_status)

    p_expire = sub.add_parser(
        "expire-due", help="persistir vencimientos por fecha (todos los tenants)"
    )
    agregar_apply(p_expire)
    p_expire.set_defaults(handler=cmd_expire_due)

    p_diagnose = sub.add_parser(
        "diagnose", help="solo lectura: tenants que los controles dejarían bloqueados"
    )
    p_diagnose.set_defaults(handler=cmd_diagnose)

    return parser.parse_args(argv)


async def main() -> None:
    args = parse_args()
    url, connect_args = async_engine_config()
    engine = create_async_engine(url, connect_args=connect_args)
    codigo = 0
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            codigo = await args.handler(session, args)
            if not getattr(args, "apply", False):
                await session.rollback()
    finally:
        await engine.dispose()
    if codigo:
        sys.exit(codigo)


if __name__ == "__main__":
    asyncio.run(main())
