"""Panel SUPERADMIN: `GET /admin/monitor` (foto agregada de la plataforma)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.business_time import today_ar
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.daily_activity import TenantDailyActivity
from app.persistence.models.tenant import Subscription, Tenant
from app.persistence.models.user import User
from app.utils.security import create_access_token, hash_password

_URL = "/api/v1/admin/monitor"


async def _headers(db: AsyncSession, tenant: Tenant, rol: str) -> dict[str, str]:
    user = User(
        user_id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        email=f"{rol.lower()}-{uuid.uuid4().hex[:6]}@vektor.app",
        full_name=f"Usuario {rol}",
        password_hash=hash_password("Secure789"),
        role_code=rol,
        is_active=True,
    )
    db.add(user)
    await db.commit()
    token = create_access_token(
        {"sub": str(user.user_id), "tenant_id": str(tenant.tenant_id), "role_code": rol}
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def superadmin_headers(db_session: AsyncSession, sample_tenant: Tenant) -> dict[str, str]:
    return await _headers(db_session, sample_tenant, "SUPERADMIN")


async def _tenant(
    db: AsyncSession,
    nombre: str,
    *,
    estado: str = "ACTIVE",
    demo: bool = False,
    dias_de_alta: int = 60,
) -> Tenant:
    t = Tenant(
        tenant_id=uuid.uuid4(),
        legal_name=f"{nombre} SA",
        display_name=nombre,
        status=estado,
        is_demo=demo,
        created_at=datetime.now(UTC) - timedelta(days=dias_de_alta),
    )
    db.add(t)
    await db.flush()
    return t


def _sub(tenant: Tenant, estado: str, **fechas: datetime) -> Subscription:
    return Subscription(
        subscription_id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        plan_code="esencial",
        status=estado,
        **fechas,
    )


async def _uso(db: AsyncSession, tenant: Tenant, hace_dias: int) -> None:
    usuario = User(
        user_id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        email=f"u-{uuid.uuid4().hex[:8]}@x.com",
        full_name="U",
        password_hash=hash_password("Secure789"),
        role_code="OWNER",
        is_active=True,
    )
    db.add(usuario)
    await db.flush()
    db.add(
        TenantDailyActivity(
            user_id=usuario.user_id,
            activity_date=today_ar() - timedelta(days=hace_dias),
            tenant_id=tenant.tenant_id,
            created_at=datetime.now(UTC),
        )
    )


# ── Autorización ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("rol", ["OWNER", "ADMIN", "VIEWER", "CASHIER"])
async def test_solo_el_superadmin_entra(
    client: AsyncClient, db_session: AsyncSession, sample_tenant: Tenant, rol: str
) -> None:
    headers = await _headers(db_session, sample_tenant, rol)
    assert (await client.get(_URL, headers=headers)).status_code == 403


async def test_sin_token_es_401(client: AsyncClient) -> None:
    assert (await client.get(_URL)).status_code == 401


# ── Conteos ────────────────────────────────────────────────────────────────────


async def test_los_demos_no_entran_en_ningun_conteo(
    client: AsyncClient, db_session: AsyncSession, superadmin_headers: dict[str, str]
) -> None:
    antes = (await client.get(_URL, headers=superadmin_headers)).json()
    demo = await _tenant(db_session, "Demo Kiosco", demo=True)
    db_session.add(_sub(demo, "GRACE", grace_ends_at=datetime.now(UTC)))
    await _uso(db_session, demo, 0)
    await db_session.commit()

    despues = (await client.get(_URL, headers=superadmin_headers)).json()
    assert despues["negocios"] == antes["negocios"]
    assert despues["suscripciones"] == antes["suscripciones"]
    assert despues["actividad"] == antes["actividad"]
    assert "Demo Kiosco" not in {a["display_name"] for a in despues["atencion"]}


async def test_negocios_por_estado_de_cuenta(
    client: AsyncClient, db_session: AsyncSession, superadmin_headers: dict[str, str]
) -> None:
    antes = (await client.get(_URL, headers=superadmin_headers)).json()["negocios"]
    await _tenant(db_session, "N1", estado="TRIAL")
    await _tenant(db_session, "N2", estado="READ_ONLY")
    await _tenant(db_session, "N3", estado="SUSPENDED")
    await _tenant(db_session, "N4", estado="ACTIVE")
    await db_session.commit()

    n = (await client.get(_URL, headers=superadmin_headers)).json()["negocios"]
    assert n["total"] == antes["total"] + 4
    assert n["trial"] == antes["trial"] + 1
    assert n["read_only"] == antes["read_only"] + 1
    assert n["suspended"] == antes["suspended"] + 1
    assert n["active"] == antes["active"] + 1


async def test_suscripciones_vivas_y_vencimientos(
    client: AsyncClient, db_session: AsyncSession, superadmin_headers: dict[str, str]
) -> None:
    antes = (await client.get(_URL, headers=superadmin_headers)).json()["suscripciones"]
    ahora = datetime.now(UTC)
    pronto = await _tenant(db_session, "Vence pronto")
    lejos = await _tenant(db_session, "Vence lejos")
    cancelada = await _tenant(db_session, "Cancelada")
    db_session.add_all(
        [
            _sub(pronto, "ACTIVE", current_period_end=ahora + timedelta(days=3)),
            _sub(lejos, "ACTIVE", current_period_end=ahora + timedelta(days=40)),
            _sub(cancelada, "CANCELLED", current_period_end=ahora - timedelta(days=1)),
        ]
    )
    await db_session.commit()

    s = (await client.get(_URL, headers=superadmin_headers)).json()["suscripciones"]
    assert s["active"] == antes["active"] + 2
    assert s["vencen_en_7_dias"] == antes["vencen_en_7_dias"] + 1
    # La cancelada no es una suscripción viva: el negocio cuenta como sin suscripción.
    assert s["sin_suscripcion"] == antes["sin_suscripcion"] + 1


async def test_actividad_por_ventana(
    client: AsyncClient, db_session: AsyncSession, superadmin_headers: dict[str, str]
) -> None:
    antes = (await client.get(_URL, headers=superadmin_headers)).json()["actividad"]
    hoy = await _tenant(db_session, "Usó hoy")
    semana = await _tenant(db_session, "Usó hace 5 días")
    mes = await _tenant(db_session, "Usó hace 20 días")
    viejo = await _tenant(db_session, "Usó hace 45 días")
    await _uso(db_session, hoy, 0)
    await _uso(db_session, semana, 5)
    await _uso(db_session, mes, 20)
    await _uso(db_session, viejo, 45)
    await db_session.commit()

    a = (await client.get(_URL, headers=superadmin_headers)).json()["actividad"]
    assert a["hoy"] == antes["hoy"] + 1
    assert a["ultimos_7_dias"] == antes["ultimos_7_dias"] + 2
    assert a["ultimos_30_dias"] == antes["ultimos_30_dias"] + 3


# ── Atención requerida ─────────────────────────────────────────────────────────


async def test_atencion_motivos_y_orden(
    client: AsyncClient, db_session: AsyncSession, superadmin_headers: dict[str, str]
) -> None:
    ahora = datetime.now(UTC)
    ro = await _tenant(db_session, "Vencido")
    gr = await _tenant(db_session, "En gracia")
    tr = await _tenant(db_session, "Prueba por terminar")
    tr_ok = await _tenant(db_session, "Prueba holgada")
    ina = await _tenant(db_session, "Sin uso")
    nuevo = await _tenant(db_session, "Recién creado", dias_de_alta=2)
    activo = await _tenant(db_session, "Con uso reciente")
    suspendido = await _tenant(db_session, "Suspendido sin uso", estado="SUSPENDED")
    db_session.add_all(
        [
            _sub(ro, "READ_ONLY", current_period_end=ahora - timedelta(days=10)),
            _sub(gr, "GRACE", grace_ends_at=ahora + timedelta(days=2)),
            _sub(tr, "TRIAL", trial_ends_at=ahora + timedelta(days=1)),
            _sub(tr_ok, "TRIAL", trial_ends_at=ahora + timedelta(days=12)),
        ]
    )
    await _uso(db_session, activo, 1)
    # Los que tienen suscripción también tienen uso: sólo `ina` y `suspendido` no.
    for t in (ro, gr, tr, tr_ok):
        await _uso(db_session, t, 1)
    await db_session.commit()

    r = (await client.get(_URL, headers=superadmin_headers)).json()
    por_nombre = {(a["display_name"], a["motivo"]) for a in r["atencion"]}
    assert ("Vencido", "READ_ONLY") in por_nombre
    assert ("En gracia", "GRACE") in por_nombre
    assert ("Prueba por terminar", "TRIAL_ENDING") in por_nombre
    assert ("Sin uso", "INACTIVE") in por_nombre
    # No entran: prueba holgada, negocio nuevo, con uso reciente, ni suspendido.
    nombres = {n for n, _ in por_nombre}
    excluidos = {"Prueba holgada", "Recién creado", "Con uso reciente", "Suspendido sin uso"}
    assert not nombres & excluidos
    # Lo más grave primero.
    motivos = [a["motivo"] for a in r["atencion"]]
    orden = ["READ_ONLY", "GRACE", "TRIAL_ENDING", "INACTIVE"]
    assert motivos == sorted(motivos, key=orden.index)
    assert r["atencion_total"] >= 4
    _ = (ina, nuevo, suspendido)


async def test_la_lista_de_atencion_se_recorta_pero_informa_el_total(
    client: AsyncClient, db_session: AsyncSession, superadmin_headers: dict[str, str]
) -> None:
    for i in range(25):
        await _tenant(db_session, f"Sin uso {i:02d}")
    await db_session.commit()

    r = (await client.get(_URL, headers=superadmin_headers)).json()
    assert len(r["atencion"]) == 20
    assert r["atencion_total"] >= 25


# ── IA ─────────────────────────────────────────────────────────────────────────


async def test_ia_sin_consumo_dice_cuando_fue_el_ultimo(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    superadmin_headers: dict[str, str],
) -> None:
    sin = (await client.get(_URL, headers=superadmin_headers)).json()["ia"]
    assert sin["tokens_total"] == 0
    assert sin["ultimo_consumo"] is None

    viejo = datetime.now(UTC) - timedelta(days=100)
    db_session.add(
        DecisionAuditLog(
            id=uuid.uuid4(),
            tenant_id=sample_tenant.tenant_id,
            decision_type="AGENT_CHAT",
            decision_data={},
            triggered_by="agent:chat",
            tokens_input=10,
            tokens_output=5,
            tokens_total=15,
            created_at=viejo,
        )
    )
    await db_session.commit()

    ia = (await client.get(_URL, headers=superadmin_headers)).json()["ia"]
    # Fuera de los 30 días: no suma, pero el "último consumo" sí lo recuerda.
    assert ia["tokens_total"] == 0
    assert ia["ultimo_consumo"] == viejo.date().isoformat()
