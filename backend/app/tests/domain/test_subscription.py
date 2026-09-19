"""`effective_access()` — puro, sin DB. Fechas fijas a propósito."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.domain.subscription import (
    TRIAL_LIMITS,
    PlanQuota,
    SubscriptionStatus,
    effective_access,
)

_NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
_GRANTED_CONTROL = PlanQuota(
    seats_included=3, ia_queries_per_month=70, imports_per_month=25, photo_pdf_reads_per_month=7
)


def test_trial_vigente_usa_trial_limits_no_el_plan_asignado() -> None:
    acceso = effective_access(
        status=SubscriptionStatus.TRIAL.value,
        granted_quota=_GRANTED_CONTROL,  # el plan asignado post-trial es Control
        created_at=_NOW - timedelta(days=1),
        trial_ends_at=_NOW + timedelta(days=13),
        grace_ends_at=None,
        current_period_start=None,
        current_period_end=None,
        now=_NOW,
    )
    assert acceso.blocked_reason is None
    assert acceso.quota == TRIAL_LIMITS  # nunca los cupos de Control


def test_trial_vencida_bloquea_por_fecha_no_por_status() -> None:
    acceso = effective_access(
        status=SubscriptionStatus.TRIAL.value,  # el string todavía dice TRIAL
        granted_quota=_GRANTED_CONTROL,
        created_at=_NOW - timedelta(days=20),
        trial_ends_at=_NOW - timedelta(days=6),  # venció hace 6 días
        grace_ends_at=None,
        current_period_start=None,
        current_period_end=None,
        now=_NOW,
    )
    assert acceso.blocked_reason == "prueba_vencida"


def test_active_normal_usa_el_periodo_y_cupo_otorgado() -> None:
    inicio = _NOW - timedelta(days=5)
    fin = _NOW + timedelta(days=25)
    acceso = effective_access(
        status=SubscriptionStatus.ACTIVE.value,
        granted_quota=_GRANTED_CONTROL,
        created_at=_NOW - timedelta(days=40),
        trial_ends_at=None,
        grace_ends_at=None,
        current_period_start=inicio,
        current_period_end=fin,
        now=_NOW,
    )
    assert acceso.blocked_reason is None
    assert acceso.quota == _GRANTED_CONTROL
    assert (acceso.period_start, acceso.period_end) == (inicio, fin)


def test_active_sin_periodo_seteado_cae_al_mes_calendario() -> None:
    """Activación manual pendiente de un `renew` explícito: no inventa fechas
    fuera del mes en curso, pero tampoco bloquea — ACTIVE sin período es un
    estado transitorio válido, no un error."""
    acceso = effective_access(
        status=SubscriptionStatus.ACTIVE.value,
        granted_quota=_GRANTED_CONTROL,
        created_at=_NOW,
        trial_ends_at=None,
        grace_ends_at=None,
        current_period_start=None,
        current_period_end=None,
        now=_NOW,
    )
    assert acceso.blocked_reason is None
    assert acceso.period_start.day == 1
    assert acceso.period_start.month == 9
    assert acceso.period_end.month == 10  # primer día del mes siguiente


def test_grace_conserva_el_mismo_periodo_no_acredita_uno_nuevo() -> None:
    inicio = _NOW - timedelta(days=35)
    fin = _NOW - timedelta(days=5)  # el período pago ya terminó
    acceso = effective_access(
        status=SubscriptionStatus.GRACE.value,
        granted_quota=_GRANTED_CONTROL,
        created_at=_NOW - timedelta(days=100),
        trial_ends_at=None,
        grace_ends_at=fin + timedelta(days=7),
        current_period_start=inicio,
        current_period_end=fin,
        now=_NOW,
    )
    assert acceso.blocked_reason is None
    # el período reportado es el VIEJO, no uno nuevo que arranque hoy
    assert (acceso.period_start, acceso.period_end) == (inicio, fin)


def test_grace_vencida_bloquea() -> None:
    inicio = _NOW - timedelta(days=40)
    fin = _NOW - timedelta(days=10)
    acceso = effective_access(
        status=SubscriptionStatus.GRACE.value,
        granted_quota=_GRANTED_CONTROL,
        created_at=_NOW - timedelta(days=100),
        trial_ends_at=None,
        grace_ends_at=fin + timedelta(days=7),  # venció hace 3 días
        current_period_start=inicio,
        current_period_end=fin,
        now=_NOW,
    )
    assert acceso.blocked_reason == "gracia_vencida"


def test_grace_sin_periodo_bloquea_no_hay_nada_que_conservar() -> None:
    acceso = effective_access(
        status=SubscriptionStatus.GRACE.value,
        granted_quota=_GRANTED_CONTROL,
        created_at=_NOW,
        trial_ends_at=None,
        grace_ends_at=None,
        current_period_start=None,
        current_period_end=None,
        now=_NOW,
    )
    assert acceso.blocked_reason == "gracia_sin_periodo"


def test_read_only_siempre_bloquea() -> None:
    acceso = effective_access(
        status=SubscriptionStatus.READ_ONLY.value,
        granted_quota=_GRANTED_CONTROL,
        created_at=_NOW - timedelta(days=60),
        trial_ends_at=None,
        grace_ends_at=None,
        current_period_start=_NOW - timedelta(days=20),
        current_period_end=_NOW + timedelta(days=10),
        now=_NOW,
    )
    assert acceso.blocked_reason == "solo_lectura"


def test_cancelled_con_periodo_vigente_no_bloquea() -> None:
    acceso = effective_access(
        status=SubscriptionStatus.CANCELLED.value,
        granted_quota=_GRANTED_CONTROL,
        created_at=_NOW - timedelta(days=60),
        trial_ends_at=None,
        grace_ends_at=None,
        current_period_start=_NOW - timedelta(days=5),
        current_period_end=_NOW + timedelta(days=25),  # todavía pago
        now=_NOW,
    )
    assert acceso.blocked_reason is None


def test_cancelled_con_periodo_vencido_bloquea() -> None:
    acceso = effective_access(
        status=SubscriptionStatus.CANCELLED.value,
        granted_quota=_GRANTED_CONTROL,
        created_at=_NOW - timedelta(days=60),
        trial_ends_at=None,
        grace_ends_at=None,
        current_period_start=_NOW - timedelta(days=35),
        current_period_end=_NOW - timedelta(days=5),
        now=_NOW,
    )
    assert acceso.blocked_reason == "cancelada_periodo_vencido"


def test_cancelled_sin_periodo_bloquea() -> None:
    acceso = effective_access(
        status=SubscriptionStatus.CANCELLED.value,
        granted_quota=_GRANTED_CONTROL,
        created_at=_NOW,
        trial_ends_at=None,
        grace_ends_at=None,
        current_period_start=None,
        current_period_end=None,
        now=_NOW,
    )
    assert acceso.blocked_reason == "cancelada_periodo_vencido"


def test_naive_datetime_tira_typeerror_en_vez_de_comparar_mal() -> None:
    import pytest  # noqa: PLC0415

    with pytest.raises(TypeError):
        effective_access(
            status=SubscriptionStatus.TRIAL.value,
            granted_quota=_GRANTED_CONTROL,
            created_at=datetime(2026, 9, 1),  # naive, sin tzinfo
            trial_ends_at=datetime(2026, 9, 15),
            grace_ends_at=None,
            current_period_start=None,
            current_period_end=None,
            now=_NOW,
        )
