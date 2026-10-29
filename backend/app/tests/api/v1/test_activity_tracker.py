"""Telemetría de uso (panel SUPERADMIN, F5): un usuario, un día, una fila."""

from __future__ import annotations

import uuid
from datetime import timedelta

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import activity_tracker
from app.application.services.activity_tracker import (
    registrar_actividad,
    resumen_de_actividad,
)
from app.domain.business_time import now_ar, today_ar
from app.persistence.models.daily_activity import TenantDailyActivity
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.utils.security import create_access_token, hash_password

_ME = "/api/v1/auth/me"


async def _filas(db: AsyncSession) -> list[TenantDailyActivity]:
    return list((await db.execute(select(TenantDailyActivity))).scalars().all())


async def test_una_peticion_autenticada_registra_el_dia(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_user: User,
    auth_headers: dict[str, str],
) -> None:
    assert (await client.get(_ME, headers=auth_headers)).status_code == 200
    filas = await _filas(db_session)
    assert [(f.user_id, f.activity_date, f.tenant_id) for f in filas] == [
        (sample_user.user_id, today_ar(), sample_user.tenant_id)
    ]


async def test_varias_peticiones_el_mismo_dia_no_duplican(
    client: AsyncClient,
    db_session: AsyncSession,
    auth_headers: dict[str, str],
) -> None:
    for _ in range(3):
        assert (await client.get(_ME, headers=auth_headers)).status_code == 200
    assert len(await _filas(db_session)) == 1


async def test_el_superadmin_no_cuenta(
    client: AsyncClient, db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    user = User(
        user_id=uuid.uuid4(),
        tenant_id=sample_tenant.tenant_id,
        email="super@vektor.app",
        full_name="Soporte",
        password_hash=hash_password("Secure789"),
        role_code="SUPERADMIN",
        is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    token = create_access_token(
        {
            "sub": str(user.user_id),
            "tenant_id": str(sample_tenant.tenant_id),
            "role_code": "SUPERADMIN",
        }
    )
    r = await client.get(_ME, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert await _filas(db_session) == []


async def test_cuenta_suspendida_no_registra_uso(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    auth_headers: dict[str, str],
) -> None:
    sample_tenant.status = "SUSPENDED"
    await db_session.commit()
    assert (await client.get(_ME, headers=auth_headers)).status_code == 403
    assert await _filas(db_session) == []


async def test_el_set_en_memoria_se_marca_solo_tras_un_commit_real(
    db_session: AsyncSession, sample_user: User
) -> None:
    activity_tracker._vistos.clear()
    clave = (sample_user.user_id, today_ar())
    db_session.expunge(sample_user)  # el rollback expira lo cargado: no releerlo

    await registrar_actividad(db_session, sample_user)
    assert clave not in activity_tracker._vistos  # todavía sin commit

    await db_session.rollback()
    assert clave not in activity_tracker._vistos  # revertida: no se esconde
    assert await _filas(db_session) == []

    await registrar_actividad(db_session, sample_user)
    await db_session.commit()
    assert clave in activity_tracker._vistos
    assert len(await _filas(db_session)) == 1


async def test_un_fallo_de_telemetria_no_rompe_la_peticion(
    db_session: AsyncSession, sample_user: User, monkeypatch
) -> None:
    activity_tracker._vistos.clear()

    def _roto(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(activity_tracker, "_insertar", _roto)
    await registrar_actividad(db_session, sample_user)  # no levanta
    assert await _filas(db_session) == []


async def test_resumen_ultimo_dia_y_dias_activos_en_ventana(
    db_session: AsyncSession, sample_user: User, sample_tenant: Tenant
) -> None:
    hoy = today_ar()
    for atras in (0, 2, 5, 45):
        db_session.add(
            TenantDailyActivity(
                user_id=sample_user.user_id,
                activity_date=hoy - timedelta(days=atras),
                tenant_id=sample_tenant.tenant_id,
                created_at=now_ar(),
            )
        )
    await db_session.commit()
    sin_uso = uuid.uuid4()
    r = await resumen_de_actividad(db_session, [sample_tenant.tenant_id, sin_uso])
    assert r[sample_tenant.tenant_id] == (hoy, 3)  # el de hace 45 días queda fuera
    assert r[sin_uso] == (None, 0)
    assert await resumen_de_actividad(db_session, []) == {}
    conteo = select(func.count()).select_from(TenantDailyActivity)
    total = (await db_session.execute(conteo)).scalar()
    assert total == 4


# ── Lo que ve el panel ─────────────────────────────────────────────────────────


async def test_el_panel_muestra_ultimo_uso_y_dias_activos(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_user: User,
    sample_tenant: Tenant,
    second_tenant: Tenant,
) -> None:
    hoy = today_ar()
    for atras in (1, 3):
        db_session.add(
            TenantDailyActivity(
                user_id=sample_user.user_id,
                activity_date=hoy - timedelta(days=atras),
                tenant_id=sample_tenant.tenant_id,
                created_at=now_ar(),
            )
        )
    soporte = User(
        user_id=uuid.uuid4(),
        tenant_id=sample_tenant.tenant_id,
        email="soporte@vektor.app",
        full_name="Soporte",
        password_hash=hash_password("Secure789"),
        role_code="SUPERADMIN",
        is_active=True,
    )
    db_session.add(soporte)
    await db_session.commit()
    token = create_access_token(
        {
            "sub": str(soporte.user_id),
            "tenant_id": str(sample_tenant.tenant_id),
            "role_code": "SUPERADMIN",
        }
    )
    headers = {"Authorization": f"Bearer {token}"}

    r = await client.get("/api/v1/admin/tenants", headers=headers)
    assert r.status_code == 200, r.text
    items = {i["tenant_id"]: i for i in r.json()["items"]}
    con_uso = items[str(sample_tenant.tenant_id)]
    assert con_uso["last_activity_date"] == (hoy - timedelta(days=1)).isoformat()
    assert con_uso["active_days_30d"] == 2  # el soporte no suma
    sin_uso = items[str(second_tenant.tenant_id)]
    assert sin_uso["last_activity_date"] is None
    assert sin_uso["active_days_30d"] == 0

    ficha = await client.get(f"/api/v1/admin/tenants/{sample_tenant.tenant_id}", headers=headers)
    assert ficha.status_code == 200, ficha.text
    assert ficha.json()["last_activity_date"] == (hoy - timedelta(days=1)).isoformat()
    assert ficha.json()["active_days_30d"] == 2
