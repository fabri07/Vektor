"""Un usuario dado de baja no se reactiva con la verificación de email (X3-1).

Revisión profunda: ``DELETE /users/{id}`` dejaba la cuenta en ``is_active=False``,
el mismo estado que un registro pendiente de verificar. ``resend-verification``
le mandaba un link y ``verify-email`` la activaba y devolvía un JWT: un ex
empleado volvía solo, con su rol y sus permisos, sin pasar por el dueño ni por
el cupo de plazas. Ahora la baja marca ``deactivated_at`` y ninguno de los dos
actúa sobre ella; el registro pendiente sigue verificándose como siempre.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import auth_service
from app.application.services.auth_service import AuthService
from app.config.settings import get_settings
from app.domain.verticals import Vertical
from app.persistence.models.auth_token import EmailVerificationToken
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.utils.security import hash_password


async def _empleado(db: AsyncSession, tenant: Tenant) -> User:
    user = User(
        user_id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        email=f"empleado-{uuid.uuid4().hex[:6]}@kiosco.com",
        full_name="Ex Empleado",
        password_hash=hash_password("Secure123"),
        role_code="ADMIN",
        is_active=True,
        can_modify_sensitive=True,
    )
    db.add(user)
    await db.commit()
    return user


async def _tokens(db: AsyncSession, user: User) -> int:
    return int(
        (
            await db.execute(
                select(func.count(EmailVerificationToken.token_id)).where(
                    EmailVerificationToken.user_id == user.user_id
                )
            )
        ).scalar_one()
    )


async def test_baja_reenvio_y_verificacion_no_reactivan(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    auth_headers: dict[str, str],
) -> None:
    empleado = await _empleado(db_session, sample_tenant)

    baja = await client.delete(f"/api/v1/users/{empleado.user_id}", headers=auth_headers)
    assert baja.status_code == 200
    await db_session.refresh(empleado)
    assert empleado.is_active is False
    assert empleado.deactivated_at is not None

    # El reenvío responde lo mismo que siempre (no filtra existencia), pero no
    # emite token ni manda mail.
    with patch.object(AuthService, "_send_verification_email") as mail:
        reenvio = await client.post(
            "/api/v1/auth/resend-verification", json={"email": empleado.email}
        )
    assert reenvio.status_code == 200
    mail.assert_not_called()
    assert await _tokens(db_session, empleado) == 0

    # Un token que ya tenía (emitido antes del arreglo, o antes de la baja)
    # tampoco la reactiva: mismo 400 genérico, sin JWT, sin consumirlo.
    token = EmailVerificationToken(
        token_id=uuid.uuid4(),
        user_id=empleado.user_id,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        used=False,
    )
    db_session.add(token)
    await db_session.commit()

    verificacion = await client.post(
        "/api/v1/auth/verify-email", json={"token": str(token.token_id)}
    )
    assert verificacion.status_code == 400
    assert verificacion.json()["detail"] == "invalid_or_expired_token"
    assert "access_token" not in verificacion.text

    await db_session.refresh(empleado)
    await db_session.refresh(token)
    assert empleado.is_active is False
    assert empleado.deactivated_at is not None
    assert token.used is False

    # Y sigue sin poder entrar.
    login = await client.post(
        "/api/v1/auth/login", json={"email": empleado.email, "password": "Secure123"}
    )
    assert login.status_code != 200
    assert "access_token" not in login.text


async def test_registro_pendiente_se_verifica_y_activa(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El caso permitido: un registro que espera su email sí se activa."""
    monkeypatch.setattr(get_settings(), "ENABLE_OPEN_REGISTRATION", True)
    monkeypatch.setattr(auth_service.settings, "ENABLE_EMAIL_VERIFICATION", True)
    email = f"nuevo-{uuid.uuid4().hex[:6]}@kiosco.example.com"

    with patch.object(AuthService, "_send_verification_email") as mail:
        alta = await client.post(
            "/api/v1/auth/register",
            json={
                "email": email,
                "password": "Secure123",
                "full_name": "Dueña Nueva",
                "business_name": "Kiosco Nuevo",
                "vertical_code": Vertical.KIOSCO_ALMACEN.value,
            },
        )
        assert alta.status_code == 201, alta.text
        reenvio = await client.post("/api/v1/auth/resend-verification", json={"email": email})
    assert reenvio.status_code == 200
    # El registro y el reenvío mandan su mail: el reenvío sigue andando.
    assert mail.call_count == 2

    user = (await db_session.execute(select(User).where(User.email == email))).scalar_one()
    assert user.is_active is False
    assert user.deactivated_at is None
    ultimo = (
        await db_session.execute(
            select(EmailVerificationToken)
            .where(
                EmailVerificationToken.user_id == user.user_id,
                EmailVerificationToken.used.is_(False),
            )
        )
    ).scalar_one()

    verificacion = await client.post(
        "/api/v1/auth/verify-email", json={"token": str(ultimo.token_id)}
    )
    assert verificacion.status_code == 200, verificacion.text
    assert verificacion.json()["access_token"]

    await db_session.refresh(user)
    await db_session.refresh(ultimo)
    assert user.is_active is True
    assert ultimo.used is True
