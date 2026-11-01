"""Monitor de plataforma del SUPERADMIN: una foto de todos los negocios.

Sólo lee y sólo agrega lo que ya existe; no calcula métricas de negocio nuevas:
estados de cuenta (`tenants.status`), suscripciones vivas, telemetría diaria de
uso (`tenant_daily_activity`) y consumo de IA (`usage_service`). Los demos quedan
fuera de los conteos de negocios, de suscripciones, de actividad y de atención:
inflarían la foto con cuentas que no son clientes. El consumo de IA es global a
propósito, porque el costo de un demo también se paga.

El volumen es el de la cartera de clientes (decenas, no millones), así que se
trae una fila por negocio y se agrega en Python con un número FIJO de consultas.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Final

from sqlalchemy import Row, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import usage_service
from app.domain.business_time import today_ar
from app.domain.subscription import TERMINAL_STATUSES, SubscriptionStatus
from app.domain.tenant_status import (
    ACTIVE,
    ESTADOS_OPERATIVOS,
    READ_ONLY,
    SUSPENDED,
    TRIAL,
    normalizar,
)
from app.persistence.models.daily_activity import TenantDailyActivity
from app.persistence.models.tenant import Subscription, Tenant
from app.schemas.admin_monitor import (
    MonitorActividad,
    MonitorAtencion,
    MonitorIA,
    MonitorNegocios,
    MonitorResponse,
    MonitorSuscripciones,
    MotivoDeAtencion,
)

#: Un negocio operativo sin un solo día de uso en este lapso pide atención.
UMBRAL_DIAS_SIN_USO: Final[int] = 14
#: Una prueba que termina dentro de este lapso (o ya terminó sin cerrarse) también.
UMBRAL_DIAS_FIN_DE_PRUEBA: Final[int] = 3
#: Ventana de "períodos pagos que vencen pronto".
VENTANA_VENCIMIENTOS_DIAS: Final[int] = 7
VENTANA_IA_DIAS: Final[int] = 30
MAX_ATENCION: Final[int] = 20

# Más grave primero: es el orden de la lista.
_ORDEN_MOTIVO: Final[dict[str, int]] = {
    "READ_ONLY": 0,
    "GRACE": 1,
    "TRIAL_ENDING": 2,
    "INACTIVE": 3,
}


def _aware(valor: datetime | None) -> datetime | None:
    """Fechas sin zona (SQLite en tests) se tratan como UTC."""
    if valor is not None and valor.tzinfo is None:
        return valor.replace(tzinfo=UTC)
    return valor


async def get_monitor(session: AsyncSession) -> MonitorResponse:
    ahora = datetime.now(UTC)
    hoy = today_ar()

    negocios = (
        await session.execute(
            select(Tenant.tenant_id, Tenant.display_name, Tenant.status, Tenant.created_at).where(
                Tenant.is_demo.is_(False)
            )
        )
    ).all()
    ids_reales = {n.tenant_id for n in negocios}

    # Suscripción VIVA: el índice único parcial garantiza a lo sumo una por tenant.
    subs = {
        s.tenant_id: s
        for s in (
            await session.execute(
                select(
                    Subscription.tenant_id,
                    Subscription.status,
                    Subscription.current_period_end,
                    Subscription.trial_ends_at,
                    Subscription.grace_ends_at,
                ).where(Subscription.status.not_in(TERMINAL_STATUSES))
            )
        ).all()
        if s.tenant_id in ids_reales
    }

    ultimo_uso: dict[uuid.UUID, date] = {
        fila.tenant_id: fila.ultimo
        for fila in (
            await session.execute(
                select(
                    TenantDailyActivity.tenant_id,
                    func.max(TenantDailyActivity.activity_date).label("ultimo"),
                ).group_by(TenantDailyActivity.tenant_id)
            )
        ).all()
        if fila.tenant_id in ids_reales
    }

    # ── Negocios por estado de cuenta ────────────────────────────────────────
    estados = [normalizar(n.status) for n in negocios]
    n_negocios = MonitorNegocios(
        total=len(negocios),
        active=estados.count(ACTIVE),
        trial=estados.count(TRIAL),
        read_only=estados.count(READ_ONLY),
        suspended=estados.count(SUSPENDED),
    )

    # ── Suscripciones vivas ──────────────────────────────────────────────────
    estados_sub = [s.status for s in subs.values()]
    limite_venc = ahora + timedelta(days=VENTANA_VENCIMIENTOS_DIAS)
    vencen = 0
    for s in subs.values():
        fin = _aware(s.current_period_end)
        es_activa = s.status == SubscriptionStatus.ACTIVE.value
        if es_activa and fin is not None and ahora <= fin <= limite_venc:
            vencen += 1
    n_subs = MonitorSuscripciones(
        trial=estados_sub.count(SubscriptionStatus.TRIAL.value),
        active=estados_sub.count(SubscriptionStatus.ACTIVE.value),
        grace=estados_sub.count(SubscriptionStatus.GRACE.value),
        read_only=estados_sub.count(SubscriptionStatus.READ_ONLY.value),
        sin_suscripcion=len(ids_reales - set(subs)),
        vencen_en_7_dias=vencen,
    )

    # ── Actividad (el soporte no cuenta: el tracker ya lo excluye al escribir) ─
    actividad = MonitorActividad(
        hoy=sum(1 for d in ultimo_uso.values() if d >= hoy),
        ultimos_7_dias=sum(1 for d in ultimo_uso.values() if d >= hoy - timedelta(days=6)),
        ultimos_30_dias=sum(1 for d in ultimo_uso.values() if d >= hoy - timedelta(days=29)),
    )

    # ── Atención requerida ───────────────────────────────────────────────────
    items: list[MonitorAtencion] = []
    corte_sin_uso = hoy - timedelta(days=UMBRAL_DIAS_SIN_USO - 1)
    corte_alta = ahora - timedelta(days=UMBRAL_DIAS_SIN_USO)
    limite_prueba = ahora + timedelta(days=UMBRAL_DIAS_FIN_DE_PRUEBA)
    for n in negocios:
        sub = subs.get(n.tenant_id)
        if sub is not None:
            if sub.status == SubscriptionStatus.READ_ONLY.value:
                items.append(_item(n, "READ_ONLY", _aware(sub.current_period_end)))
            elif sub.status == SubscriptionStatus.GRACE.value:
                items.append(_item(n, "GRACE", _aware(sub.grace_ends_at)))
            elif sub.status == SubscriptionStatus.TRIAL.value:
                fin_prueba = _aware(sub.trial_ends_at)
                if fin_prueba is not None and fin_prueba <= limite_prueba:
                    items.append(_item(n, "TRIAL_ENDING", fin_prueba))
        uso = ultimo_uso.get(n.tenant_id)
        alta = _aware(n.created_at)
        if (
            normalizar(n.status) in ESTADOS_OPERATIVOS
            and alta is not None
            and alta <= corte_alta
            and (uso is None or uso < corte_sin_uso)
        ):
            items.append(_item(n, "INACTIVE", uso))
    items.sort(key=lambda i: (_ORDEN_MOTIVO[i.motivo], _clave_fecha(i), i.display_name))

    return MonitorResponse(
        generado_en=ahora,
        negocios=n_negocios,
        suscripciones=n_subs,
        actividad=actividad,
        ia=await _ia(session),
        atencion=items[:MAX_ATENCION],
        atencion_total=len(items),
    )


def _item(
    n: Row[tuple[uuid.UUID, str, str, datetime]],
    motivo: MotivoDeAtencion,
    fecha: datetime | date | None,
) -> MonitorAtencion:
    return MonitorAtencion(
        tenant_id=n.tenant_id, display_name=n.display_name, motivo=motivo, fecha=fecha
    )


def _clave_fecha(item: MonitorAtencion) -> float:
    """Más viejo primero; sin fecha al final de su grupo."""
    f = item.fecha
    if f is None:
        return float("inf")
    if isinstance(f, datetime):
        return f.replace(tzinfo=f.tzinfo or UTC).timestamp()
    return datetime(f.year, f.month, f.day, tzinfo=UTC).timestamp()


async def _ia(session: AsyncSession) -> MonitorIA:
    uso = await usage_service.get_usage_dashboard(session, days=VENTANA_IA_DIAS, tenant_id=None)
    return MonitorIA(
        dias=VENTANA_IA_DIAS,
        tokens_total=uso.totals.tokens_total,
        cost_usd=uso.totals.cost_usd,
        unpriced_tokens=uso.totals.unpriced_tokens,
        ultimo_consumo=uso.last_consumption_date,
    )
