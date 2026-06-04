"""Tests del WorkScheduleService (Sprint 20)."""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.work_schedule_service import (
    DEFAULT_CLOSE_HOUR,
    DEFAULT_OPEN_HOUR,
    DEFAULT_WORK_DAYS,
    WorkScheduleRequest,
    WorkScheduleService,
    resolve_schedule,
)
from app.persistence.models.business import BusinessProfile
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.utils.security import hash_password


async def _seed_profile(db: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    tid = uuid.uuid4()
    uid = uuid.uuid4()
    db.add(
        Tenant(
            tenant_id=tid,
            legal_name="T",
            display_name="T",
            currency="ARS",
            pricing_reference_mode="MEP",
            status="ACTIVE",
        )
    )
    db.add(
        User(
            user_id=uid,
            tenant_id=tid,
            email="o@t.com",
            full_name="O",
            password_hash=hash_password("Secure123"),
            role_code="OWNER",
            is_active=True,
        )
    )
    db.add(
        BusinessProfile(
            tenant_id=tid,
            vertical_code="kiosco_almacen",
        )
    )
    await db.commit()
    return tid, uid


def test_resolve_schedule_defaults_when_none():
    assert resolve_schedule(None) == (
        DEFAULT_WORK_DAYS,
        DEFAULT_OPEN_HOUR,
        DEFAULT_CLOSE_HOUR,
    )


def test_resolve_schedule_keeps_explicit_zero():
    # día 0 (lunes) y hora 0 son válidos; nunca deben caer a default por falsy.
    bp = BusinessProfile(tenant_id=uuid.uuid4(), vertical_code="kiosco_almacen")
    bp.work_days = [0]
    bp.work_open_hour = 0
    bp.work_close_hour = 23
    assert resolve_schedule(bp) == ([0], 0, 23)


@pytest.mark.asyncio
async def test_get_serves_defaults(db_session: AsyncSession):
    tid, _ = await _seed_profile(db_session)
    svc = WorkScheduleService(db_session)
    resp = await svc.get(tid)
    assert resp.work_days == DEFAULT_WORK_DAYS
    assert resp.work_open_hour == DEFAULT_OPEN_HOUR
    assert resp.work_close_hour == DEFAULT_CLOSE_HOUR
    assert resp.is_default is True


@pytest.mark.asyncio
async def test_update_persists_and_audits(db_session: AsyncSession):
    tid, uid = await _seed_profile(db_session)
    svc = WorkScheduleService(db_session)
    body = WorkScheduleRequest(
        work_days=[0, 1, 2, 3, 4], work_open_hour=8, work_close_hour=20
    )
    resp = await svc.update(tid, uid, body)
    assert resp.work_days == [0, 1, 2, 3, 4]
    assert resp.work_open_hour == 8
    assert resp.work_close_hour == 20
    assert resp.is_default is False

    # vuelve a leer: persistió
    again = await svc.get(tid)
    assert again.work_open_hour == 8
    assert again.is_default is False

    # audit registrado
    from sqlalchemy import select  # noqa: PLC0415

    from app.persistence.models.audit import DecisionAuditLog  # noqa: PLC0415

    rows = (
        await db_session.execute(
            select(DecisionAuditLog).where(
                DecisionAuditLog.tenant_id == tid,
                DecisionAuditLog.decision_type == "WORK_SCHEDULE_UPDATED",
            )
        )
    ).scalars().all()
    assert len(rows) == 1


def test_request_rejects_close_le_open():
    with pytest.raises(ValueError):
        WorkScheduleRequest(work_days=[0], work_open_hour=18, work_close_hour=9)


def test_request_rejects_out_of_range_days():
    with pytest.raises(ValueError):
        WorkScheduleRequest(work_days=[0, 7], work_open_hour=9, work_close_hour=18)


def test_request_dedupes_days():
    body = WorkScheduleRequest(work_days=[2, 0, 0, 1], work_open_hour=9, work_close_hour=18)
    assert body.work_days == [0, 1, 2]
