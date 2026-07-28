"""Canje del prefill de "Continuar con Google" en el formulario público.

El flujo real son DOS llamadas en secuencia, y este archivo las ejercita así:

1. ``GET /access-requests/prefill/{token}`` — el formulario muestra el email (y
   el nombre, si Google lo mandó). **No consume el token.**
2. ``POST /access-requests`` con el mismo token — acá se resuelve el
   ``google_subject`` y **recién acá** se consume.

Si el GET borrara la key, el POST llegaría con un token inexistente y la
solicitud quedaría sin ligar a la identidad de Google: el usuario aprobado no
podría entrar con Google y nadie se enteraría. Por eso el orden se testea
completo y no cada llamada por separado.

El encolado de mails se intercepta (mismo motivo que en
``test_access_requests.py``): sin eso, ``_encolar`` intentaría hablar con el
broker real.
"""

from __future__ import annotations

import json
import unittest.mock
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import access_request_service
from app.persistence.models.access_request import AccessRequest

URL = "/api/v1/access-requests"

_SUBJECT = "google-sub-prefill-1"
_EMAIL = "ana@kiosco.example.com"
_NOMBRE = "Ana Gómez"

_PAYLOAD: dict[str, Any] = {
    "full_name": _NOMBRE,
    "email": _EMAIL,
    "phone": None,
    "business_name": "Kiosco El Rápido",
    "requested_vertical": "kiosco_almacen",
    "requested_plan": "free",
    "years_operating": "2y_5y",
    "staff_size": "2_5",
    "monthly_revenue_band": "3m_10m",
    "main_concern": "MARGIN",
    "records_format": "planilla",
    "history_depth": "1y_3y",
    "can_share_files": "si_ordenados",
    "records_notes": None,
    "applicant_notes": None,
    "consent": True,
    "cta_source": "google_login",
    "website": "",
    "elapsed_ms": 9000,
}


class _RedisConGetdel:
    """Fake mínimo de Redis con el GETDEL que usa el canje del prefill.

    El ``FakeRedis`` de ``conftest`` no lo tiene, y justamente GETDEL es la
    operación que define el single-use del token.
    """

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, *, ex: int | None = None) -> bool:
        self.store[key] = value
        return True

    async def getdel(self, key: str) -> str | None:
        return self.store.pop(key, None)


@pytest.fixture
def redis_prefill() -> _RedisConGetdel:
    return _RedisConGetdel()


@pytest_asyncio.fixture
async def client_prefill(
    db_session: AsyncSession, redis_prefill: _RedisConGetdel
) -> AsyncGenerator[AsyncClient, None]:
    from app.main import create_app, limiter
    from app.persistence.db.redis_client import get_redis
    from app.persistence.db.session import get_db_session

    limiter._storage.reset()
    app = create_app()

    async def _session() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    async def _redis() -> AsyncGenerator[_RedisConGetdel, None]:
        yield redis_prefill

    app.dependency_overrides[get_db_session] = _session
    app.dependency_overrides[get_redis] = _redis

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


@pytest.fixture(autouse=True)
def encolar() -> Any:
    with unittest.mock.patch.object(access_request_service, "_encolar") as mock:
        yield mock


def _sembrar(redis: _RedisConGetdel, token: str, *, email: str = _EMAIL) -> None:
    """Deja un prefill vigente, con la misma forma que escribe el callback."""
    redis.store[f"oauth:prefill:{token}"] = json.dumps(
        {
            "provider": "google",
            "provider_subject": _SUBJECT,
            "email": email,
            "full_name": _NOMBRE,
        }
    )


async def _solicitud(session: AsyncSession, email: str) -> AccessRequest:
    return (
        (
            await session.execute(
                select(AccessRequest).where(AccessRequest.email == email)
            )
        )
        .scalars()
        .one()
    )


# ── El flujo real: GET y después POST ────────────────────────────────────────


async def test_get_no_consume_el_token_y_el_post_liga_el_subject(
    client_prefill: AsyncClient,
    redis_prefill: _RedisConGetdel,
    db_session: AsyncSession,
) -> None:
    """LA secuencia del PR: prefill → formulario → solicitud ligada a Google."""
    _sembrar(redis_prefill, "tok-1")

    prefill = await client_prefill.get(f"{URL}/prefill/tok-1")
    assert prefill.status_code == 200
    assert prefill.json() == {
        "email": _EMAIL,
        "full_name": _NOMBRE,
        "provider": "google",
    }
    # El identificador de la identidad no viaja al browser.
    assert "provider_subject" not in prefill.json()

    # Sobrevive al GET: el formulario puede releerlo (recarga, doble render).
    assert (await client_prefill.get(f"{URL}/prefill/tok-1")).status_code == 200

    alta = await client_prefill.post(URL, json={**_PAYLOAD, "google_prefill_token": "tok-1"})
    assert alta.status_code == 201

    solicitud = await _solicitud(db_session, _EMAIL)
    assert solicitud.google_subject == _SUBJECT

    # Y recién el POST lo consumió.
    assert (await client_prefill.get(f"{URL}/prefill/tok-1")).status_code == 404


async def test_el_token_es_single_use(
    client_prefill: AsyncClient,
    redis_prefill: _RedisConGetdel,
    db_session: AsyncSession,
) -> None:
    """Canjeado una vez, no liga una segunda solicitud."""
    _sembrar(redis_prefill, "tok-2")
    await client_prefill.post(URL, json={**_PAYLOAD, "google_prefill_token": "tok-2"})

    otro = "otro@negocio.example.com"
    segunda = await client_prefill.post(
        URL,
        json={**_PAYLOAD, "email": otro, "google_prefill_token": "tok-2"},
    )

    # Se acepta igual (el token vencido no tira a la basura el formulario)…
    assert segunda.status_code == 201
    # …pero sin inventar un subject.
    assert (await _solicitud(db_session, otro)).google_subject is None


async def test_token_vencido_no_bloquea_la_solicitud(
    client_prefill: AsyncClient, db_session: AsyncSession
) -> None:
    """El TTL es de 10 min y el formulario es largo: no se pierde lo contestado."""
    alta = await client_prefill.post(
        URL, json={**_PAYLOAD, "google_prefill_token": "token-que-vencio"}
    )

    assert alta.status_code == 201
    assert (await _solicitud(db_session, _EMAIL)).google_subject is None


async def test_una_solicitud_ya_abierta_adopta_el_subject(
    client_prefill: AsyncClient,
    redis_prefill: _RedisConGetdel,
    db_session: AsyncSession,
) -> None:
    """El caso que se perdía en silencio: primero el formulario, después Google.

    El token se consume antes de saber si `create()` va a crear una fila nueva o
    a derivar al reintento, así que el reintento tiene que adoptar el subject o
    la identidad se pierde sin que ningún log lo mencione.
    """
    assert (await client_prefill.post(URL, json=_PAYLOAD)).status_code == 201
    assert (await _solicitud(db_session, _EMAIL)).google_subject is None

    _sembrar(redis_prefill, "tok-abierta")
    segunda = await client_prefill.post(
        URL, json={**_PAYLOAD, "google_prefill_token": "tok-abierta"}
    )

    assert segunda.status_code == 201
    solicitud = await _solicitud(db_session, _EMAIL)  # sigue habiendo UNA sola
    await db_session.refresh(solicitud)
    assert solicitud.google_subject == _SUBJECT


async def test_sin_token_la_solicitud_no_queda_ligada(
    client_prefill: AsyncClient, db_session: AsyncSession
) -> None:
    """El alta del formulario público a secas no toca `google_subject`."""
    assert (await client_prefill.post(URL, json=_PAYLOAD)).status_code == 201
    assert (await _solicitud(db_session, _EMAIL)).google_subject is None


# ── Guardias ─────────────────────────────────────────────────────────────────


async def test_token_de_otro_email_es_403_y_no_persiste_nada(
    client_prefill: AsyncClient,
    redis_prefill: _RedisConGetdel,
    db_session: AsyncSession,
) -> None:
    """Mismo agujero que tapa `complete_link` con `email_mismatch`.

    Sin este chequeo, quien controla una cuenta de Google podría ligar su
    identidad a una solicitud hecha con el email de otra persona.
    """
    _sembrar(redis_prefill, "tok-3", email="verificado@gmail.com")

    resp = await client_prefill.post(
        URL,
        json={**_PAYLOAD, "email": "otra.persona@gmail.com", "google_prefill_token": "tok-3"},
    )

    assert resp.status_code == 403
    assert resp.json()["detail"] == "google_prefill_email_mismatch"
    assert (
        await db_session.execute(select(AccessRequest))
    ).scalars().all() == []


async def test_prefill_inexistente_devuelve_404(client_prefill: AsyncClient) -> None:
    resp = await client_prefill.get(f"{URL}/prefill/no-existe")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "prefill_invalido_o_expirado"
