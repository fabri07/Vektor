"""Tests del formulario público de solicitud de acceso (/api/v1/access-requests).

La aserción central de todo el PR está en `test_create_no_crea_ninguna_cuenta`:
**después de un alta exitosa, Tenant / User / Subscription / BusinessProfile /
MomentumProfile siguen en 0**. Es lo que prueba que el registro dejó de crear
cuentas; si esa aserción se afloja, la feature entera dejó de existir aunque el
resto siga verde.

Después: neutralidad a enumeración (mismo cuerpo exista o no la cuenta),
idempotencia, anti-bot, la validación de `vertical_other_text` no-vacío,
`extra="forbid"` contra un payload con `password`, y el doble opt-in.

`_encolar` se mockea porque el worker Celery (`app/jobs/access_request_worker`)
lo agrega la tarea siguiente: hoy el import falla y el servicio lo absorbe en
silencio (por diseño — encolar nunca puede romper un alta ya persistida). Se
mockea para poder ASERTAR qué se encoló, no para que el test pase.
"""

from __future__ import annotations

import unittest.mock
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import access_request_service
from app.application.services.access_request_service import CreateOutcome
from app.domain.access_request import AccessRequestStatus, RequestedPlan
from app.persistence.models.access_request import AccessRequest, AccessRequestToken
from app.persistence.models.business import BusinessProfile, MomentumProfile
from app.persistence.models.tenant import Subscription, Tenant
from app.persistence.models.user import User
from app.utils.security import hash_password

URL = "/api/v1/access-requests"
URL_VERIFY = f"{URL}/verify"
URL_RESEND = f"{URL}/resend"

_VALIDO: dict[str, Any] = {
    "full_name": "Juan Pérez",
    "email": "Juan@Kiosco.EXAMPLE.com",
    "phone": "+54 11 5555-1234",
    "business_name": "Kiosco El Rápido",
    "requested_vertical": "kiosco_almacen",
    "requested_plan": "free",
    "years_operating": "2y_5y",
    "staff_size": "2_5",
    "monthly_revenue_band": "3m_10m",
    "main_concern": "MARGIN",
    "records_format": "planilla",
    "history_depth": "1y_3y",
    "can_share_files": "si_desprolijos",
    "records_notes": "Anoto en un cuaderno y paso los totales al Excel.",
    "applicant_notes": None,
    "consent": True,
    "cta_source": "landing_hero",
    "website": "",
    "elapsed_ms": 5000,
}

#: Las cinco tablas que acuña un alta de cuenta. Un alta de SOLICITUD no debe
#: tocar ninguna.
_TABLAS_DE_CUENTA = (Tenant, User, Subscription, BusinessProfile, MomentumProfile)


async def _contar(session: AsyncSession, modelo: type) -> int:
    return int(
        (await session.execute(select(func.count()).select_from(modelo))).scalar_one()
    )


async def _solicitudes(session: AsyncSession) -> int:
    return await _contar(session, AccessRequest)


@pytest.fixture
def encolar() -> Any:
    """Intercepta el encolado de mails del servicio."""
    with unittest.mock.patch.object(access_request_service, "_encolar") as mock:
        yield mock


async def _crear_cuenta_con_email(session: AsyncSession, email: str) -> None:
    """Un tenant + user reales con ese email (para el caso 'ya tenés cuenta')."""
    tenant = Tenant(
        tenant_id=uuid.uuid4(),
        legal_name="Ya Existe SRL",
        display_name="Ya Existe SRL",
        currency="ARS",
        pricing_reference_mode="MEP",
        status="ACTIVE",
    )
    session.add(tenant)
    await session.flush()
    session.add(
        User(
            user_id=uuid.uuid4(),
            tenant_id=tenant.tenant_id,
            email=email,
            full_name="Titular Existente",
            password_hash=hash_password("Secure123"),
            role_code="OWNER",
            is_active=True,
        )
    )
    await session.commit()


# ── La aserción central ───────────────────────────────────────────────────────


async def test_create_no_crea_ninguna_cuenta(
    client: AsyncClient, db_session: AsyncSession, encolar: Any
) -> None:
    """LA aserción del PR: una solicitud aceptada NO acuña ninguna de las 5 filas.

    Si esto falla, el registro abierto volvió por la ventana.
    """
    res = await client.post(URL, json=_VALIDO)
    assert res.status_code == 201, res.text

    assert await _solicitudes(db_session) == 1
    for modelo in _TABLAS_DE_CUENTA:
        assert await _contar(db_session, modelo) == 0, (
            f"Una solicitud de acceso creó filas en {modelo.__name__}: "
            "el alta pública NO puede crear cuentas."
        )


async def test_create_persiste_la_solicitud_normalizada(
    client: AsyncClient, db_session: AsyncSession, encolar: Any
) -> None:
    res = await client.post(URL, json=_VALIDO)
    assert res.status_code == 201

    fila = (await db_session.execute(select(AccessRequest))).scalar_one()
    # Normalización: email en minúsculas, teléfono compacto.
    assert fila.email == "juan@kiosco.example.com"
    assert fila.phone == "+541155551234"
    assert fila.status == AccessRequestStatus.UNVERIFIED.value
    assert fila.requested_plan == RequestedPlan.FREE.value
    assert fila.consent_version  # el backend sella su propia versión
    assert fila.ip_hash is None or len(fila.ip_hash) == 64
    # Se emitió el token del doble opt-in y se encoló el mail de verificación.
    assert await _contar(db_session, AccessRequestToken) == 1
    encolar.assert_called_once()
    assert encolar.call_args.args[0] == access_request_service.TASK_VERIFICACION


# ── Neutralidad a enumeración de cuentas ──────────────────────────────────────


async def test_cuerpo_identico_exista_o_no_la_cuenta(
    client: AsyncClient, db_session: AsyncSession, encolar: Any
) -> None:
    """Mismo status y mismo cuerpo, exista o no una cuenta con ese email.

    El `POST /auth/register` viejo devolvía 409 "An account with this email
    already exists": un oráculo de enumeración. Este endpoint es anónimo y no
    puede repetirlo.
    """
    sin_cuenta = await client.post(URL, json={**_VALIDO, "email": "nueva@example.com"})

    await _crear_cuenta_con_email(db_session, "ocupada@example.com")
    con_cuenta = await client.post(URL, json={**_VALIDO, "email": "ocupada@example.com"})

    assert sin_cuenta.status_code == con_cuenta.status_code == 201
    assert sin_cuenta.json() == con_cuenta.json()

    # Y con cuenta existente NO se persistió ninguna solicitud (solo la primera).
    filas = (await db_session.execute(select(AccessRequest))).scalars().all()
    assert [f.email for f in filas] == ["nueva@example.com"]
    # El único canal que distingue el caso es la casilla del dueño del email.
    tareas = [c.args[0] for c in encolar.call_args_list]
    assert access_request_service.TASK_CUENTA_EXISTENTE in tareas


async def test_doble_envio_crea_una_sola_solicitud(
    client: AsyncClient, db_session: AsyncSession, encolar: Any
) -> None:
    primera = await client.post(URL, json=_VALIDO)
    segunda = await client.post(URL, json=_VALIDO)

    assert primera.status_code == segunda.status_code == 201
    assert primera.json() == segunda.json()
    assert await _solicitudes(db_session) == 1
    # El cooldown de reemisión evita un mail por click.
    encolar.assert_called_once()


# ── Anti-bot ──────────────────────────────────────────────────────────────────


async def test_honeypot_descarta_en_silencio(
    client: AsyncClient, db_session: AsyncSession, encolar: Any
) -> None:
    """El honeypot completado ⇒ 201 al bot y CERO filas.

    ⚠️ El email del bot tiene que ser DISTINTO del legítimo. Con el mismo email,
    un honeypot roto daría igual una sola fila —el bot insertaría y el envío
    legítimo caería en `DUPLICATE_OPEN`—, y el test pasaría sin poder fallar
    nunca. Por eso el conteo en cero se asierta ANTES de mandar el legítimo.
    """
    bot = await client.post(
        URL,
        json={
            **_VALIDO,
            "email": "bot@spam.example",
            "website": "http://spam.example",
        },
    )
    assert bot.status_code == 201
    # Lo que realmente prueba que el honeypot funciona: no persistió NADA.
    assert await _solicitudes(db_session) == 0
    encolar.assert_not_called()

    # Y al bot se le respondió exactamente lo mismo que a un envío legítimo.
    legitimo = await client.post(URL, json=_VALIDO)
    assert legitimo.status_code == 201
    assert bot.json() == legitimo.json()

    # La única fila es la legítima, no la del bot.
    filas = (await db_session.execute(select(AccessRequest))).scalars().all()
    assert [f.email for f in filas] == ["juan@kiosco.example.com"]


async def test_envio_demasiado_rapido_descartado(
    client: AsyncClient, db_session: AsyncSession, encolar: Any
) -> None:
    res = await client.post(URL, json={**_VALIDO, "elapsed_ms": 100})
    assert res.status_code == 201
    assert await _solicitudes(db_session) == 0
    encolar.assert_not_called()


# ── Validación del payload ────────────────────────────────────────────────────


async def test_password_en_el_payload_es_422(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """`extra="forbid"`: un bundle viejo que manda `password` se entera.

    Sin esto, el campo se ignoraría en silencio y el visitante creería que dio de
    alta una cuenta con esa contraseña.
    """
    res = await client.post(URL, json={**_VALIDO, "password": "Secure123"})
    assert res.status_code == 422
    assert await _solicitudes(db_session) == 0


async def test_campo_desconocido_es_422(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    res = await client.post(URL, json={**_VALIDO, "vertical_code": "kiosco_almacen"})
    assert res.status_code == 422
    assert await _solicitudes(db_session) == 0


@pytest.mark.parametrize("texto", [None, "", "   ", "ab"])
async def test_otros_sin_texto_util_es_422(
    client: AsyncClient, db_session: AsyncSession, texto: str | None
) -> None:
    """El CHECK de la base deja pasar `''`; el schema no.

    `requested_vertical='otros'` con `vertical_other_text=''` satisface
    `requested_vertical <> 'otros' OR vertical_other_text IS NOT NULL` y dejaría
    la solicitud sin la única información que justifica la opción "Otro".
    """
    res = await client.post(
        URL,
        json={
            **_VALIDO,
            "requested_vertical": "otros",
            "vertical_other_text": texto,
        },
    )
    assert res.status_code == 422, res.text
    assert await _solicitudes(db_session) == 0


async def test_otros_con_texto_es_aceptado(
    client: AsyncClient, db_session: AsyncSession, encolar: Any
) -> None:
    res = await client.post(
        URL,
        json={
            **_VALIDO,
            "requested_vertical": "otros",
            "vertical_other_text": "  Ferretería de barrio  ",
        },
    )
    assert res.status_code == 201
    fila = (await db_session.execute(select(AccessRequest))).scalar_one()
    assert fila.requested_vertical == "otros"
    assert fila.vertical_other_text == "Ferretería de barrio"  # strippeado


async def test_texto_de_otro_rubro_sin_otros_es_422(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    res = await client.post(URL, json={**_VALIDO, "vertical_other_text": "algo"})
    assert res.status_code == 422
    assert await _solicitudes(db_session) == 0


async def test_requested_plan_ausente_es_422(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Requerido y sin default: un `free` implícito inventaría una respuesta."""
    payload = {k: v for k, v in _VALIDO.items() if k != "requested_plan"}
    res = await client.post(URL, json=payload)
    assert res.status_code == 422
    assert await _solicitudes(db_session) == 0


async def test_requested_plan_desconocido_es_422(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    res = await client.post(URL, json={**_VALIDO, "requested_plan": "enterprise"})
    assert res.status_code == 422
    assert await _solicitudes(db_session) == 0


async def test_sin_consentimiento_es_422(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    res = await client.post(URL, json={**_VALIDO, "consent": False})
    assert res.status_code == 422
    assert await _solicitudes(db_session) == 0


async def test_email_invalido_es_422(client: AsyncClient, db_session: AsyncSession) -> None:
    res = await client.post(URL, json={**_VALIDO, "email": "no-es-un-email"})
    assert res.status_code == 422
    assert await _solicitudes(db_session) == 0


async def test_banda_de_screening_desconocida_es_422(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    res = await client.post(URL, json={**_VALIDO, "years_operating": "hace_mucho"})
    assert res.status_code == 422
    assert await _solicitudes(db_session) == 0


async def test_telefono_opcional_pero_valido_si_viene(
    client: AsyncClient, db_session: AsyncSession, encolar: Any
) -> None:
    sin_telefono = await client.post(
        URL, json={**_VALIDO, "email": "a@example.com", "phone": None}
    )
    assert sin_telefono.status_code == 201

    basura = await client.post(URL, json={**_VALIDO, "email": "b@example.com", "phone": "abc"})
    assert basura.status_code == 422

    filas = (await db_session.execute(select(AccessRequest))).scalars().all()
    assert len(filas) == 1
    assert filas[0].phone is None


# ── Doble opt-in ──────────────────────────────────────────────────────────────


async def _token_vigente(session: AsyncSession) -> uuid.UUID:
    token = (
        await session.execute(
            select(AccessRequestToken).where(AccessRequestToken.used.is_(False))
        )
    ).scalar_one()
    return token.token_id


async def test_verify_pasa_la_solicitud_a_pending(
    client: AsyncClient, db_session: AsyncSession, encolar: Any
) -> None:
    await client.post(URL, json=_VALIDO)
    token = await _token_vigente(db_session)

    res = await client.post(URL_VERIFY, json={"token": str(token)})
    assert res.status_code == 200, res.text
    assert res.json()["requested_plan"] == RequestedPlan.FREE.value

    db_session.expire_all()
    fila = (await db_session.execute(select(AccessRequest))).scalar_one()
    assert fila.status == AccessRequestStatus.PENDING.value
    assert fila.email_verified_at is not None
    # Recién ahí se le avisa al dueño que hay algo para revisar.
    tareas = [c.args[0] for c in encolar.call_args_list]
    assert access_request_service.TASK_AVISO_DUENIO in tareas


async def test_verify_dos_veces_responde_200_las_dos(
    client: AsyncClient, db_session: AsyncSession, encolar: Any
) -> None:
    """Doble click en el mail, prefetchers y escáneres de spam lo hacen siempre."""
    await client.post(URL, json=_VALIDO)
    token = str(await _token_vigente(db_session))

    primera = await client.post(URL_VERIFY, json={"token": token})
    segunda = await client.post(URL_VERIFY, json={"token": token})

    assert primera.status_code == 200
    assert segunda.status_code == 200
    assert primera.json() == segunda.json()

    db_session.expire_all()
    fila = (await db_session.execute(select(AccessRequest))).scalar_one()
    verificado_una_sola_vez = [
        c.args[0] for c in encolar.call_args_list
    ].count(access_request_service.TASK_AVISO_DUENIO)
    assert verificado_una_sola_vez == 1
    assert fila.status == AccessRequestStatus.PENDING.value


async def test_verify_con_token_inexistente_es_400(client: AsyncClient) -> None:
    res = await client.post(URL_VERIFY, json={"token": str(uuid.uuid4())})
    assert res.status_code == 400
    assert res.json()["detail"] == "token_invalido_o_expirado"


async def test_verify_con_token_basura_es_400(client: AsyncClient) -> None:
    res = await client.post(URL_VERIFY, json={"token": "no-es-un-uuid"})
    assert res.status_code == 400


async def test_verify_sin_token_es_422(client: AsyncClient) -> None:
    res = await client.post(URL_VERIFY, json={"token": ""})
    assert res.status_code == 422


# ── Reenvío ───────────────────────────────────────────────────────────────────


async def test_resend_responde_igual_haya_o_no_solicitud(
    client: AsyncClient, encolar: Any
) -> None:
    desconocido = await client.post(URL_RESEND, json={"email": "nadie@example.com"})
    assert desconocido.status_code == 200

    await client.post(URL, json=_VALIDO)
    # El cooldown del token acaba de empezar, así que este reenvío es un no-op…
    conocido = await client.post(URL_RESEND, json={"email": _VALIDO["email"]})
    assert conocido.status_code == 200
    # …y aun así responde EXACTAMENTE lo mismo que para un email desconocido.
    assert desconocido.json() == conocido.json()


async def test_resend_email_invalido_es_422(client: AsyncClient) -> None:
    res = await client.post(URL_RESEND, json={"email": "no-es-un-email"})
    assert res.status_code == 422


# ── Rate limit ────────────────────────────────────────────────────────────────


async def test_rate_limit_del_alta(client: AsyncClient, encolar: Any) -> None:
    """5/hora por IP; el 6º corta con 429."""
    codigos = [
        (await client.post(URL, json={**_VALIDO, "email": f"sol{i}@example.com"})).status_code
        for i in range(6)
    ]
    assert codigos[:5] == [201] * 5
    assert codigos[5] == 429


async def test_rate_limit_del_reenvio(client: AsyncClient) -> None:
    """3/15min por IP: es el endpoint que dispara mails sin captcha."""
    codigos = [
        (await client.post(URL_RESEND, json={"email": f"x{i}@example.com"})).status_code
        for i in range(4)
    ]
    assert codigos[:3] == [200] * 3
    assert codigos[3] == 429


async def test_rate_limit_del_verify(client: AsyncClient) -> None:
    """10/5min por IP: frena el brute force de tokens."""
    codigos = [
        (await client.post(URL_VERIFY, json={"token": str(uuid.uuid4())})).status_code
        for i in range(11)
    ]
    assert codigos[:10] == [400] * 10
    assert codigos[10] == 429


# ── Regla cardinal ────────────────────────────────────────────────────────────


async def test_la_solicitud_persiste_aunque_falle_el_encolado(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Un broker caído no puede perder una solicitud ni romperle la respuesta al visitante.

    Se rompe la resolución del worker (lo que `_encolar` hace por dentro), no
    `_encolar` mismo: mockear la función entera se saltearía justo el `try/except`
    que estamos verificando.
    """
    with unittest.mock.patch.object(
        access_request_service,
        "import_module",
        side_effect=RuntimeError("broker caído"),
    ):
        res = await client.post(URL, json=_VALIDO)
    assert res.status_code == 201
    assert await _solicitudes(db_session) == 1


# ── Apagado del registro abierto ──────────────────────────────────────────────


_REGISTRO = "/api/v1/auth/register"
_ONBOARDING = "/api/v1/onboarding/submit"
_REGISTER_PAYLOAD = {
    "email": "owner@kiosco.example.com",
    "password": "Secure123",
    "full_name": "Juan Pérez",
    "business_name": "Kiosco El Rápido",
    "vertical_code": "kiosco_almacen",
}


async def test_register_responde_410_y_no_crea_cuenta(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """La ruta NO se borró: devuelve 410 con un código estable.

    Un bundle viejo del frontend recibe una señal accionable en vez de un 404 o un
    422 de esquema, y no crea ninguna cuenta.
    """
    res = await client.post(_REGISTRO, json=_REGISTER_PAYLOAD)
    assert res.status_code == 410, res.text
    assert res.json()["detail"] == "registration_closed"
    for modelo in _TABLAS_DE_CUENTA:
        assert await _contar(db_session, modelo) == 0


async def test_register_410_gana_sobre_el_422_de_esquema(client: AsyncClient) -> None:
    """El payload basura de un bundle viejo tiene que ver el 410, no un 422.

    Por eso la compuerta va como `dependencies=[...]` y no como primera línea del
    cuerpo: se resuelve antes de validar el body.
    """
    res = await client.post(_REGISTRO, json={"basura": True})
    assert res.status_code == 410
    assert res.json()["detail"] == "registration_closed"


async def test_onboarding_submit_no_esta_gateado_por_el_registro(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    """`POST /onboarding/submit` NO se apaga, con el flag en su default (`False`).

    La encuesta se partió en dos a propósito: el formulario público es de
    SCREENING (lo lee el dueño para decidir la admisión) y los 6 números
    financieros se piden DESPUÉS de aprobar, en el primer login. Este endpoint
    está detrás de JWT, lo usa un usuario YA aprobado y no crea ninguna cuenta —
    no es una pieza del registro abierto.

    Gatearlo dejaría a todo tenant aprobado sin poder completar el onboarding, con
    el health score nunca calculándose. Este test existe para que nadie lo vuelva
    a gatear dentro de tres meses leyendo una versión vieja del plan.
    """
    from app.config.settings import get_settings

    assert get_settings().ENABLE_OPEN_REGISTRATION is False, (
        "El test tiene que correr con el default de producción"
    )

    res = await client.post(
        _ONBOARDING,
        json={
            "vertical_code": "kiosco_almacen",
            "weekly_sales_estimate_ars": 350000,
            "monthly_inventory_cost_ars": 180000,
            "monthly_fixed_expenses_ars": 80000,
            "cash_on_hand_ars": 150000,
            "product_count_estimate": 45,
            "supplier_count_estimate": 3,
            "main_concern": "CASH",
        },
        headers=auth_headers,
    )
    assert res.status_code != 410, "El apagado del registro NO alcanza a /onboarding/submit"
    assert res.status_code == 200, res.text


async def test_onboarding_status_sigue_vivo(client: AsyncClient) -> None:
    """`GET /onboarding/status` tampoco se apaga: `/auth/me` y los guards lo leen.

    Sin token es 401 (no 410): la compuerta del registro no lo toca.
    """
    res = await client.get("/api/v1/onboarding/status")
    assert res.status_code == 401


async def test_el_flag_devuelve_el_registro_abierto(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ENABLE_OPEN_REGISTRATION=True` es el rollback de una línea.

    Si esta aserción falla, el apagado dejó de ser reversible y pasó a ser un
    borrado encubierto.
    """
    from app.config.settings import get_settings

    monkeypatch.setattr(get_settings(), "ENABLE_OPEN_REGISTRATION", True)
    res = await client.post(_REGISTRO, json=_REGISTER_PAYLOAD)
    assert res.status_code == 201, res.text
    assert await _contar(db_session, Tenant) == 1


async def test_outcome_nunca_viaja_en_la_respuesta(
    client: AsyncClient, db_session: AsyncSession, encolar: Any
) -> None:
    """Ningún valor de `CreateOutcome` puede aparecer en el cuerpo público."""
    await _crear_cuenta_con_email(db_session, "ocupada@example.com")
    cuerpos = [
        (await client.post(URL, json={**_VALIDO, "email": "libre@example.com"})).text,
        (await client.post(URL, json={**_VALIDO, "email": "ocupada@example.com"})).text,
        (await client.post(URL, json={**_VALIDO, "website": "http://spam"})).text,
    ]
    for cuerpo in cuerpos:
        for desenlace in CreateOutcome:
            assert desenlace.value not in cuerpo
