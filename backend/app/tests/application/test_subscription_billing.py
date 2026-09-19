"""Bloque D — el servicio comercial: activar, renovar, cambiar de plan, cancelar.

Reloj controlado (`now=`) en todo: las reglas son de fechas. La carrera de dos
pagos simultáneos con la misma referencia está en
`integration/test_subscription_quota_pg.py` (necesita Postgres de verdad).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import subscription_billing_service as billing
from app.application.services import subscription_service as svc
from app.domain.subscription import QuotaResource, add_months_anchored
from app.persistence.models.tenant import (
    PlanDefinition,
    Subscription,
    SubscriptionPayment,
    Tenant,
)
from app.persistence.models.user import User

T0 = datetime(2026, 1, 31, 15, 0, tzinfo=UTC)  # un 31, a propósito
DIA = timedelta(days=1)


# ── Fechas (puro) ─────────────────────────────────────────────────────────────


def test_el_dia_ancla_no_se_pierde_en_los_meses_cortos() -> None:
    """31 ene → 28 feb → 31 mar → 30 abr. Si el ajuste se arrastrara (28 → 28)
    el cliente perdería días para siempre y no coincidiría con un débito."""
    feb = add_months_anchored(T0, anchor_day=31)
    mar = add_months_anchored(feb, anchor_day=31)
    abr = add_months_anchored(mar, anchor_day=31)
    assert (feb.month, feb.day) == (2, 28)
    assert (mar.month, mar.day) == (3, 31)
    assert (abr.month, abr.day) == (4, 30)
    assert feb.hour == 15 and feb.tzinfo is UTC


def test_los_meses_cruzan_el_anio() -> None:
    dic = datetime(2026, 12, 15, tzinfo=UTC)
    assert add_months_anchored(dic, anchor_day=15) == datetime(2027, 1, 15, tzinfo=UTC)


# ── Arnés ─────────────────────────────────────────────────────────────────────


@pytest.fixture
async def planes(db_session: AsyncSession) -> None:
    """La suite crea el esquema con `create_all`: el seed de la migración no corre."""
    for codigo, plazas, ia in (("esencial", 1, 20), ("control", 3, 70), ("direccion", 6, 200)):
        db_session.add(
            PlanDefinition(
                plan_code=codigo,
                display_name=codigo.title(),
                seats_included=plazas,
                ia_queries_per_month=ia,
                imports_per_month=ia // 4,
                photo_pdf_reads_per_month=ia // 10,
                price_usd_reference=Decimal("27"),
                price_ars_reference=Decimal("41500"),
                is_offered=True,
            )
        )
    await db_session.commit()


@pytest.fixture
async def sub(db_session: AsyncSession, sample_tenant: Tenant, planes: None) -> Subscription:
    """El FREE del fixture pasa a una prueba, que es de donde sale un alta real."""
    await db_session.execute(
        update(Subscription)
        .where(Subscription.tenant_id == sample_tenant.tenant_id)
        .values(plan_code="control", status="TRIAL", trial_ends_at=T0 + 14 * DIA)
    )
    await db_session.commit()
    db_session.expunge_all()
    return (
        await db_session.execute(
            select(Subscription).where(Subscription.tenant_id == sample_tenant.tenant_id)
        )
    ).scalar_one()


async def _pagar(
    db: AsyncSession, sub: Subscription, expected: str, ref: str, *, now: datetime, **kw: Any
) -> billing.ResultadoDePago:
    kw.setdefault("plan_code", "control" if expected == "activate" else None)
    kw.setdefault("amount_usd", Decimal("27"))
    kw.setdefault("amount_ars", Decimal("41500"))
    resultado = await billing.apply_payment(
        db, subscription_id=sub.subscription_id, expected=expected, reference=ref,
        operator="test", now=now, **kw,
    )
    await db.commit()
    return resultado


async def _recargar(db: AsyncSession, sub: Subscription) -> Subscription:
    db.expunge_all()
    return (
        await db.execute(
            select(Subscription).where(Subscription.subscription_id == sub.subscription_id)
        )
    ).scalar_one()


# ── Activar ───────────────────────────────────────────────────────────────────


async def test_activar_arranca_hoy_fija_el_ancla_y_copia_las_condiciones(
    db_session: AsyncSession, sub: Subscription
) -> None:
    r = await _pagar(db_session, sub, "activate", "T-1", now=T0)
    s = await _recargar(db_session, sub)
    assert (r.kind, r.period_start, r.period_end.day) == (billing.KIND_ACTIVATION, T0, 28)
    assert (s.status, s.billing_anchor_day, s.seats_included) == ("ACTIVE", 31, 3)
    assert s.granted_ia_queries_per_month == 70
    pago = (await db_session.execute(select(SubscriptionPayment))).scalar_one()
    assert (pago.reference, pago.granted_json["ia_queries_per_month"]) == ("T-1", 70)


async def test_activar_a_quien_ya_tiene_un_periodo_corriendo_se_rechaza(
    db_session: AsyncSession, sub: Subscription
) -> None:
    """Era el bug de `activate`: pisaba el período en curso y, con él, el saldo."""
    await _pagar(db_session, sub, "activate", "T-1", now=T0)
    with pytest.raises(billing.BillingError) as exc:
        await _pagar(db_session, sub, "activate", "T-2", now=T0 + 10 * DIA)
    assert exc.value.codigo == "USE_RENEW"


async def test_renovar_sin_periodo_pago_se_rechaza(
    db_session: AsyncSession, sub: Subscription
) -> None:
    with pytest.raises(billing.BillingError) as exc:
        await _pagar(db_session, sub, "renew", "T-1", now=T0)
    assert exc.value.codigo == "USE_ACTIVATE"


# ── Idempotencia ──────────────────────────────────────────────────────────────


async def test_la_misma_referencia_no_otorga_dos_periodos(
    db_session: AsyncSession, sub: Subscription
) -> None:
    primero = await _pagar(db_session, sub, "activate", "T-1", now=T0)
    repetido = await _pagar(db_session, sub, "activate", "T-1", now=T0 + 3 * DIA)
    assert repetido.ya_aplicado and repetido.period_end == primero.period_end
    assert (
        await db_session.execute(select(func.count()).select_from(SubscriptionPayment))
    ).scalar_one() == 1


async def test_la_misma_referencia_con_otros_datos_es_un_error_no_un_ya_aplicada(
    db_session: AsyncSession, sub: Subscription
) -> None:
    await _pagar(db_session, sub, "activate", "T-1", now=T0)
    with pytest.raises(billing.BillingError) as exc:
        await _pagar(db_session, sub, "activate", "T-1", now=T0, amount_ars=Decimal("1"))
    assert exc.value.codigo == "REFERENCE_MISMATCH"


# ── Renovar ───────────────────────────────────────────────────────────────────


async def test_el_pago_anticipado_no_toca_el_periodo_en_curso_ni_su_saldo(
    db_session: AsyncSession, sub: Subscription
) -> None:
    """El caso NORMAL con transferencia: se paga unos días antes."""
    await _pagar(db_session, sub, "activate", "T-1", now=T0)
    s = await _recargar(db_session, sub)
    await svc.reserve(
        db_session, subscription=s, resource=QuotaResource.IA_QUERY, operation_id="x", now=T0
    )
    await svc.commit(
        db_session, tenant_id=s.tenant_id, resource=QuotaResource.IA_QUERY, operation_id="x"
    )

    antes_de_vencer = T0 + 25 * DIA
    r = await _pagar(db_session, sub, "renew", "T-2", now=antes_de_vencer)
    s = await _recargar(db_session, sub)

    assert r.kind == billing.KIND_RENEWAL_PREPAID
    assert (r.period_start.day, r.period_end.day) == (28, 31)  # 28 feb → 31 mar
    assert s.current_period_start is not None
    assert s.current_period_start.replace(tzinfo=UTC) == T0  # intacto
    acceso = svc.resolve_access(s, now=antes_de_vencer)
    assert acceso.period_start == T0  # mismo contador ⇒ mismo saldo (1 usada)

    # Al terminar el período rige el siguiente, SIN que corra ningún cron.
    en_marzo = datetime(2026, 3, 10, tzinfo=UTC)
    acceso = svc.resolve_access(s, now=en_marzo)
    assert acceso.blocked_reason is None
    assert (acceso.period_start.day, acceso.period_end.day) == (28, 31)


async def test_solo_se_acepta_un_periodo_por_adelantado(
    db_session: AsyncSession, sub: Subscription
) -> None:
    await _pagar(db_session, sub, "activate", "T-1", now=T0)
    await _pagar(db_session, sub, "renew", "T-2", now=T0 + 5 * DIA)
    with pytest.raises(billing.BillingError) as exc:
        await _pagar(db_session, sub, "renew", "T-3", now=T0 + 6 * DIA)
    assert exc.value.codigo == "NEXT_PERIOD_ALREADY_PAID"


async def test_pagar_durante_la_gracia_corre_desde_el_vencimiento_anterior(
    db_session: AsyncSession, sub: Subscription
) -> None:
    """Pagar tarde no regala días ni corre la fecha de cobro."""
    await _pagar(db_session, sub, "activate", "T-1", now=T0)
    vencio = datetime(2026, 2, 28, 15, 0, tzinfo=UTC)
    r = await _pagar(db_session, sub, "renew", "T-2", now=vencio + 4 * DIA)
    assert r.kind == billing.KIND_RENEWAL_IN_GRACE
    assert r.period_start == vencio
    assert (r.period_end.month, r.period_end.day) == (3, 31)


async def test_pasada_la_gracia_se_reactiva_desde_el_pago_con_ancla_nueva(
    db_session: AsyncSession, sub: Subscription
) -> None:
    await _pagar(db_session, sub, "activate", "T-1", now=T0)
    tarde = datetime(2026, 3, 20, 9, 0, tzinfo=UTC)  # venció el 28/2, gracia hasta el 7/3
    r = await _pagar(db_session, sub, "activate", "T-2", now=tarde)
    s = await _recargar(db_session, sub)
    assert (r.kind, r.period_start, s.billing_anchor_day) == (billing.KIND_ACTIVATION, tarde, 20)


# ── Cambio de plan ────────────────────────────────────────────────────────────


async def test_el_cambio_de_plan_rige_en_la_proxima_renovacion_sin_resetear_nada(
    db_session: AsyncSession, sub: Subscription
) -> None:
    await _pagar(db_session, sub, "activate", "T-1", now=T0)
    await billing.schedule_plan_change(
        db_session, subscription_id=sub.subscription_id, plan_code="direccion",
        operator="test", now=T0 + DIA,
    )
    await db_session.commit()
    s = await _recargar(db_session, sub)
    assert (s.plan_code, s.granted_ia_queries_per_month) == ("control", 70)  # hoy no cambia

    r = await _pagar(db_session, sub, "renew", "T-2", now=T0 + 20 * DIA)
    assert r.plan_code == "direccion"
    s = await _recargar(db_session, sub)
    assert svc.resolve_access(s, now=T0 + 21 * DIA).quota.ia_queries_per_month == 70
    assert (
        svc.resolve_access(s, now=datetime(2026, 3, 5, tzinfo=UTC)).quota.ia_queries_per_month
        == 200
    )


async def test_un_downgrade_que_deja_usuarios_sin_plaza_se_rechaza_sin_borrar_a_nadie(
    db_session: AsyncSession, sub: Subscription, sample_user: User
) -> None:
    await _pagar(db_session, sub, "activate", "T-1", now=T0)
    db_session.add(
        User(
            user_id=uuid.uuid4(), tenant_id=sub.tenant_id, email="otro@test.com",
            full_name="Otro", password_hash="x", role_code="VIEWER", is_active=True,
        )
    )
    await db_session.commit()
    with pytest.raises(billing.BillingError) as exc:
        await billing.schedule_plan_change(
            db_session, subscription_id=sub.subscription_id, plan_code="esencial",
            operator="test", now=T0 + DIA,
        )
    assert exc.value.codigo == "SEATS_EXCEEDED"
    assert "desactivar 1" in exc.value.mensaje
    activos = (
        await db_session.execute(
            select(func.count()).select_from(User).where(User.is_active.is_(True))
        )
    ).scalar_one()
    assert activos == 2


# ── Cancelar ──────────────────────────────────────────────────────────────────


async def test_cancelar_conserva_lo_pago_y_corta_sin_gracia(
    db_session: AsyncSession, sub: Subscription
) -> None:
    await _pagar(db_session, sub, "activate", "T-1", now=T0)
    hasta = await billing.schedule_cancellation(
        db_session, subscription_id=sub.subscription_id, operator="test", now=T0 + 5 * DIA
    )
    await db_session.commit()
    s = await _recargar(db_session, sub)

    assert (hasta.month, hasta.day) == (2, 28)
    assert svc.resolve_access(s, now=hasta - DIA).blocked_reason is None
    # El día que vence: cortado. Sin cancelación ahí empezarían 7 días de gracia.
    assert svc.resolve_access(s, now=hasta).blocked_reason == "cancelada_periodo_vencido"


async def test_renovar_deshace_una_cancelacion_programada(
    db_session: AsyncSession, sub: Subscription
) -> None:
    await _pagar(db_session, sub, "activate", "T-1", now=T0)
    await billing.schedule_cancellation(
        db_session, subscription_id=sub.subscription_id, operator="test", now=T0 + DIA
    )
    await _pagar(db_session, sub, "renew", "T-2", now=T0 + 2 * DIA)
    s = await _recargar(db_session, sub)
    assert s.cancel_at_period_end is False
    assert svc.resolve_access(s, now=datetime(2026, 3, 10, tzinfo=UTC)).blocked_reason is None


# ── Pase de período ───────────────────────────────────────────────────────────


async def test_roll_forward_persiste_lo_que_el_acceso_ya_resolvia_por_fecha(
    db_session: AsyncSession, sub: Subscription
) -> None:
    await _pagar(db_session, sub, "activate", "T-1", now=T0)
    await _pagar(db_session, sub, "renew", "T-2", now=T0 + 5 * DIA, plan_code="direccion")
    s = await _recargar(db_session, sub)
    en_marzo = datetime(2026, 3, 10, tzinfo=UTC)
    antes = svc.resolve_access(s, now=en_marzo)

    assert billing.roll_forward(s, now=en_marzo)
    despues = svc.resolve_access(s, now=en_marzo)

    assert (s.plan_code, s.next_period_end, s.seats_included) == ("direccion", None, 6)
    # El pase no cambia NADA de lo que ya regía: sólo lo deja escrito.
    assert (despues.period_start, despues.period_end, despues.quota) == (
        antes.period_start,
        antes.period_end,
        antes.quota,
    )
