"""Tests del PinService — setup/verify/lockout/invalidate/fail-closed."""

import uuid

import pytest
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.pin_service import (
    MAX_ATTEMPTS,
    PinAlreadySetError,
    PinInvalidError,
    PinLockedError,
    PinMismatchError,
    PinService,
)
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.utils.security import hash_password


async def _make_user(db_session: AsyncSession, *, with_pin: bool) -> User:
    tenant = Tenant(
        tenant_id=uuid.uuid4(),
        legal_name="T",
        display_name="T",
        currency="ARS",
        pricing_reference_mode="MEP",
        status="ACTIVE",
    )
    db_session.add(tenant)
    await db_session.commit()
    user = User(
        user_id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        email=f"u-{uuid.uuid4().hex[:8]}@t.com",
        full_name="U",
        password_hash=hash_password("Secure123"),
        role_code="OWNER",
        is_active=True,
        pin_hash=hash_password("4321") if with_pin else None,
    )
    db_session.add(user)
    await db_session.commit()
    return user


async def test_setup_pin_sets_hash(db_session: AsyncSession, fake_redis) -> None:
    user = await _make_user(db_session, with_pin=False)
    svc = PinService(fake_redis, db_session)
    await svc.setup_pin(user, "1234", "1234")
    assert user.pin_hash is not None
    assert user.pin_set_at is not None


async def test_setup_pin_mismatch(db_session: AsyncSession, fake_redis) -> None:
    user = await _make_user(db_session, with_pin=False)
    svc = PinService(fake_redis, db_session)
    with pytest.raises(PinMismatchError):
        await svc.setup_pin(user, "1234", "9999")


async def test_setup_pin_already_set(db_session: AsyncSession, fake_redis) -> None:
    user = await _make_user(db_session, with_pin=True)
    svc = PinService(fake_redis, db_session)
    with pytest.raises(PinAlreadySetError):
        await svc.setup_pin(user, "1234", "1234")


async def test_verify_opens_window(db_session: AsyncSession, fake_redis) -> None:
    user = await _make_user(db_session, with_pin=True)
    svc = PinService(fake_redis, db_session)
    assert await svc.is_window_valid(user.tenant_id, user.user_id) is False
    await svc.verify(user, "4321")
    assert await svc.is_window_valid(user.tenant_id, user.user_id) is True


async def test_verify_wrong_pin(db_session: AsyncSession, fake_redis) -> None:
    user = await _make_user(db_session, with_pin=True)
    svc = PinService(fake_redis, db_session)
    with pytest.raises(PinInvalidError):
        await svc.verify(user, "0000")
    assert await svc.is_window_valid(user.tenant_id, user.user_id) is False


async def test_verify_lockout_after_max_attempts(
    db_session: AsyncSession, fake_redis
) -> None:
    user = await _make_user(db_session, with_pin=True)
    svc = PinService(fake_redis, db_session)
    for _ in range(MAX_ATTEMPTS):
        with pytest.raises(PinInvalidError):
            await svc.verify(user, "0000")
    # Tras MAX_ATTEMPTS fallos, incluso el PIN correcto queda bloqueado.
    with pytest.raises(PinLockedError):
        await svc.verify(user, "4321")


async def test_change_pin_invalidates_window(
    db_session: AsyncSession, fake_redis
) -> None:
    user = await _make_user(db_session, with_pin=True)
    svc = PinService(fake_redis, db_session)
    await svc.verify(user, "4321")
    assert await svc.is_window_valid(user.tenant_id, user.user_id) is True
    await svc.change_pin(user, "4321", "5678", "5678")
    assert await svc.is_window_valid(user.tenant_id, user.user_id) is False


async def test_reset_pin_with_password(db_session: AsyncSession, fake_redis) -> None:
    user = await _make_user(db_session, with_pin=True)
    svc = PinService(fake_redis, db_session)
    await svc.reset_pin(user, "Secure123", "7777", "7777")
    # El nuevo PIN funciona; el viejo no.
    await svc.verify(user, "7777")
    assert await svc.is_window_valid(user.tenant_id, user.user_id) is True


async def test_reset_pin_wrong_password(db_session: AsyncSession, fake_redis) -> None:
    user = await _make_user(db_session, with_pin=True)
    svc = PinService(fake_redis, db_session)
    with pytest.raises(PinInvalidError):
        await svc.reset_pin(user, "WrongPass1", "7777", "7777")


async def test_is_window_valid_fail_closed(
    db_session: AsyncSession, fake_redis, monkeypatch
) -> None:
    user = await _make_user(db_session, with_pin=True)
    svc = PinService(fake_redis, db_session)
    await svc.verify(user, "4321")

    async def _boom(*_args, **_kwargs):
        raise RedisError("down")

    monkeypatch.setattr(fake_redis, "exists", _boom)
    # Redis caído al leer la ventana → fail-closed (no verificado).
    assert await svc.is_window_valid(user.tenant_id, user.user_id) is False
