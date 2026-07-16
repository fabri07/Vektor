"""Tests del formulario público de contacto (POST /api/v1/contact/leads).

Cubre: happy path, casos negativos (consentimiento faltante, email inválido,
campo muy largo), anti-spam (honeypot, envío demasiado rápido, rate limit),
idempotencia (duplicado reciente) y la REGLA CARDINAL (el lead persiste aunque
falle el encolado del email).
"""

import unittest.mock

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.contact_lead import (
    EmailNotificationStatus,
    LeadStatus,
    normalize_ar_phone,
)
from app.jobs import contact_lead_worker
from app.persistence.models.contact_lead import ContactLead

URL = "/api/v1/contact/leads"

_VALID = {
    "nombre": "Juan Pérez",
    "celular": "+54 11 5555-1234",
    "email": "Juan@Kiosco.EXAMPLE.com",
    "empresa": "Kiosco El Rápido",
    "rubro": "kiosco_almacen",
    "cantidad_usuarios": "1_5",
    "gestion_actual": "Planilla de Excel",
    "consent": True,
    "cta_source": "contacto_page",
    "website": "",
    "elapsed_ms": 5000,
}


async def _count(session: AsyncSession) -> int:
    result = await session.execute(select(func.count()).select_from(ContactLead))
    return int(result.scalar_one())


async def test_create_lead_success(client: AsyncClient, db_session: AsyncSession):
    with unittest.mock.patch.object(contact_lead_worker.notify_contact_lead, "delay") as mock:
        res = await client.post(URL, json=_VALID)
    assert res.status_code == 201
    assert res.json()["status"] == "ok"
    assert await _count(db_session) == 1

    row = (await db_session.execute(select(ContactLead))).scalar_one()
    # Normalización: email en minúsculas, teléfono compacto.
    assert row.email == "juan@kiosco.example.com"
    assert row.celular == "+541155551234"
    assert row.status == LeadStatus.NEW.value
    assert row.email_notification_status == EmailNotificationStatus.PENDING.value
    assert row.consent_version  # se guardó la versión aceptada
    assert row.ip_hash is None or len(row.ip_hash) == 64
    mock.assert_called_once()


async def test_missing_consent_rejected(client: AsyncClient, db_session: AsyncSession):
    res = await client.post(URL, json={**_VALID, "consent": False})
    assert res.status_code == 422
    assert await _count(db_session) == 0


async def test_invalid_email_rejected(client: AsyncClient, db_session: AsyncSession):
    res = await client.post(URL, json={**_VALID, "email": "no-es-un-email"})
    assert res.status_code == 422
    assert await _count(db_session) == 0


async def test_field_too_long_rejected(client: AsyncClient, db_session: AsyncSession):
    res = await client.post(URL, json={**_VALID, "nombre": "x" * 200})
    assert res.status_code == 422
    assert await _count(db_session) == 0


async def test_invalid_rubro_rejected(client: AsyncClient, db_session: AsyncSession):
    res = await client.post(URL, json={**_VALID, "rubro": "no_existe"})
    assert res.status_code == 422
    assert await _count(db_session) == 0


async def test_consent_version_is_server_authoritative(
    client: AsyncClient, db_session: AsyncSession
):
    """El cliente puede declarar una versión de consentimiento distinta, pero el
    backend persiste SIEMPRE la suya (autoritativa)."""
    from app.domain.contact_lead import CONSENT_VERSION

    with unittest.mock.patch.object(contact_lead_worker.notify_contact_lead, "delay"):
        res = await client.post(URL, json={**_VALID, "consent_version": "front-viejo"})
    assert res.status_code == 201
    row = (await db_session.execute(select(ContactLead))).scalar_one()
    assert row.consent_version == CONSENT_VERSION


async def test_honeypot_dropped(client: AsyncClient, db_session: AsyncSession):
    with unittest.mock.patch.object(contact_lead_worker.notify_contact_lead, "delay") as mock:
        res = await client.post(URL, json={**_VALID, "website": "http://spam.example"})
    # Al bot le devolvemos éxito, pero NO se persiste ni se notifica.
    assert res.status_code == 201
    assert await _count(db_session) == 0
    mock.assert_not_called()


async def test_too_fast_submit_dropped(client: AsyncClient, db_session: AsyncSession):
    with unittest.mock.patch.object(contact_lead_worker.notify_contact_lead, "delay") as mock:
        res = await client.post(URL, json={**_VALID, "elapsed_ms": 100})
    assert res.status_code == 201
    assert await _count(db_session) == 0
    mock.assert_not_called()


async def test_recent_duplicate_deduped(client: AsyncClient, db_session: AsyncSession):
    with unittest.mock.patch.object(contact_lead_worker.notify_contact_lead, "delay") as mock:
        first = await client.post(URL, json=_VALID)
        second = await client.post(URL, json=_VALID)
    assert first.status_code == 201
    assert second.status_code == 201
    # Solo un lead y una sola notificación encolada.
    assert await _count(db_session) == 1
    mock.assert_called_once()


async def test_lead_persists_even_if_enqueue_fails(
    client: AsyncClient, db_session: AsyncSession
):
    """REGLA CARDINAL: un fallo al encolar el email no pierde el lead ni rompe
    la respuesta al visitante."""
    with unittest.mock.patch.object(
        contact_lead_worker.notify_contact_lead, "delay", side_effect=RuntimeError("broker caído")
    ):
        res = await client.post(URL, json=_VALID)
    assert res.status_code == 201
    assert await _count(db_session) == 1


async def test_rate_limit_per_ip(client: AsyncClient):
    # El límite es 5/hora por IP; el 6º debe cortar con 429.
    with unittest.mock.patch.object(contact_lead_worker.notify_contact_lead, "delay"):
        codes = []
        for i in range(6):
            r = await client.post(URL, json={**_VALID, "email": f"lead{i}@example.com"})
            codes.append(r.status_code)
    assert codes[:5] == [201] * 5
    assert codes[5] == 429


def test_normalize_ar_phone():
    assert normalize_ar_phone("+54 11 5555-1234") == "+541155551234"
    assert normalize_ar_phone("(011) 5555.1234") == "01155551234"
    assert normalize_ar_phone("  ") == ""
