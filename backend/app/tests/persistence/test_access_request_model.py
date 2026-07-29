"""Smoke test del modelo de solicitudes de acceso (Task 4).

Verifica los DEFAULTS del modelo ORM sobre una fila mínima: que una solicitud
recién creada nace ``unverified`` y con los tres emails del flujo en ``pending``,
y que el token de verificación nace sin usar y con su propio UUID.

Deliberadamente NO se testean acá los CHECK ni el índice único parcial: SQLite no
valida igual que Postgres (el repo ya tiene el antecedente de un CHECK que pasó en
SQLite y rompió en Postgres) y un test verde acá sería falsa confianza. Esa
verificación va en el test de integración PG gateado por el marker ``postgres``.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.access_request import (
    ACCESS_REQUEST_TOKEN_TTL_HOURS,
    CONSENT_VERSION,
    AccessRequestStatus,
    CanShareFiles,
    HistoryDepth,
    RecordsFormat,
    RequestedPlan,
    RevenueBand,
    StaffSize,
    YearsOperating,
)
from app.domain.contact_lead import EmailNotificationStatus
from app.domain.verticals import Vertical
from app.persistence.models.access_request import AccessRequest, AccessRequestToken


def _solicitud_minima() -> AccessRequest:
    """Fila mínima válida: solo lo NOT NULL, sin tocar ningún campo con default."""
    return AccessRequest(
        full_name="Ana Gómez",
        email="ana@kiosco-sanjuan.test",
        business_name="Kiosco San Juan",
        requested_vertical=Vertical.KIOSCO_ALMACEN.value,
        requested_plan=RequestedPlan.FREE.value,
        years_operating=YearsOperating.Y2_5Y.value,
        staff_size=StaffSize.S2_5.value,
        monthly_revenue_band=RevenueBand.NO_CONTESTA.value,
        main_concern="MARGIN",
        records_format=RecordsFormat.PLANILLA.value,
        history_depth=HistoryDepth.Y1_3Y.value,
        can_share_files=CanShareFiles.SI_DESPROLIJOS.value,
        consent_version=CONSENT_VERSION,
        consent_accepted_at=datetime.now(UTC),
    )


async def test_solicitud_nace_unverified_y_con_emails_pendientes(
    db_session: AsyncSession,
) -> None:
    solicitud = _solicitud_minima()
    db_session.add(solicitud)
    await db_session.flush()

    assert solicitud.id is not None
    assert solicitud.status == AccessRequestStatus.UNVERIFIED.value
    assert solicitud.verification_email_status == EmailNotificationStatus.PENDING.value
    assert solicitud.owner_notification_status == EmailNotificationStatus.PENDING.value
    assert solicitud.decision_email_status == EmailNotificationStatus.PENDING.value


async def test_solicitud_nace_sin_verificar_ni_revisar_ni_aprobar(
    db_session: AsyncSession,
) -> None:
    """Nada del tramo de revisión/aprobación se autocompleta con un default."""
    solicitud = _solicitud_minima()
    db_session.add(solicitud)
    await db_session.flush()

    assert solicitud.email_verified_at is None
    assert solicitud.reviewed_at is None
    assert solicitud.reviewed_by_user_id is None
    assert solicitud.reviewed_via is None
    # El vertical asignado lo pone el dueño al aprobar, nunca un default.
    assert solicitud.assigned_vertical_code is None
    assert solicitud.approved_tenant_id is None
    assert solicitud.approved_user_id is None
    assert solicitud.vertical_other_text is None


async def test_token_de_verificacion_nace_sin_usar(db_session: AsyncSession) -> None:
    solicitud = _solicitud_minima()
    db_session.add(solicitud)
    await db_session.flush()

    token = AccessRequestToken(
        access_request_id=solicitud.id,
        expires_at=datetime.now(UTC)
        + timedelta(hours=ACCESS_REQUEST_TOKEN_TTL_HOURS),
    )
    db_session.add(token)
    await db_session.flush()

    # El PK ES el token que se emaila: se genera solo.
    assert token.token_id is not None
    assert token.used is False
    assert token.access_request_id == solicitud.id
