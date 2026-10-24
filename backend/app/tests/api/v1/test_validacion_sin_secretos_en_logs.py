"""Ni un 422 ni un 500 llevan contraseñas, PIN o tokens al log ni a Sentry.

Revisión profunda 2026-10-24 (E0-B-4). El handler de validación logueaba el
cuerpo crudo del request y los errores de Pydantic con su `input`: un login con
el email mal escrito, un cambio de clave débil o un PIN con letras dejaban la
contraseña o el PIN en texto plano en los logs de Railway. Al log va sólo dónde
falló y por qué; el cliente sigue recibiendo el detalle de siempre. La revisión
adversarial encontró la misma clase de fuga en el 500 (parámetros SQL con tokens
que son PK, respuestas que no validan con el `access_token` adentro) y en el
body del request que la integración de Starlette adjunta a Sentry.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, MutableMapping, Sequence
from typing import Any, cast

import pydantic
from fastapi.exceptions import ResponseValidationError
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from starlette.requests import Request
from starlette.responses import Response
from structlog.testing import capture_logs

from app.main import _respuesta_500, app, errores_para_log
from app.observability.sentry import _scrub_event
from app.persistence.db.engine_factory import crear_engine
from app.schemas.auth import PinVerifyRequest

_CLAVE = "SecretoDeLogin123"
_CLAVE_CORTA = "Ab1secr"
_CLAVE_ACTUAL = "MiClaveActual987"
_CLAVE_DEBIL = "soloslasletras"
_PIN = "12a4"
_TOKEN = "eyJhbGciOiJIUzI1NiJ9.token-secreto.firma"

_Handler = Callable[[Request, Exception], Awaitable[Response]]


def _eventos_de_validacion(
    capturados: Sequence[MutableMapping[str, Any]],
) -> list[MutableMapping[str, Any]]:
    return [
        e
        for e in capturados
        if e.get("event") in ("request.body_validation_error", "request.validation_error")
    ]


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/x", "headers": []})


async def _post_422(
    client: AsyncClient, ruta: str, body: dict[str, Any], headers: dict[str, str] | None = None
) -> list[MutableMapping[str, Any]]:
    with capture_logs() as capturados:
        resp = await client.post(ruta, json=body, headers=headers)
    assert resp.status_code == 422, resp.text
    assert _eventos_de_validacion(capturados), "el 422 tiene que loguearse"
    return capturados


# ── 422: el body del cliente ─────────────────────────────────────────────────


async def test_login_con_email_invalido_no_loguea_la_contrasena(client: AsyncClient) -> None:
    capturados = await _post_422(
        client, "/api/v1/auth/login", {"email": "no-es-un-email", "password": _CLAVE}
    )
    assert _CLAVE not in repr(capturados)
    (evento,) = _eventos_de_validacion(capturados)
    # Lo que sirve para diagnosticar sigue: qué campo y por qué.
    assert [list(e["loc"]) for e in evento["errors"]] == [["body", "email"]]
    assert "raw_body" not in evento


async def test_login_con_la_clave_invalida_no_la_loguea(client: AsyncClient) -> None:
    """El campo que falla ES la contraseña: su valor viajaba como `input`."""
    capturados = await _post_422(
        client, "/api/v1/auth/login", {"email": "ana@kiosco.com", "password": _CLAVE_CORTA}
    )
    assert _CLAVE_CORTA not in repr(capturados)


async def test_cambio_de_clave_debil_no_loguea_ninguna_de_las_dos(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    capturados = await _post_422(
        client,
        "/api/v1/auth/change-password",
        {"current_password": _CLAVE_ACTUAL, "new_password": _CLAVE_DEBIL},
        auth_headers,
    )
    assert _CLAVE_ACTUAL not in repr(capturados)
    assert _CLAVE_DEBIL not in repr(capturados)


async def test_reseteo_de_clave_debil_no_loguea_la_clave_ni_el_token(
    client: AsyncClient,
) -> None:
    capturados = await _post_422(
        client, "/api/v1/auth/reset-password", {"token": _TOKEN, "new_password": _CLAVE_DEBIL}
    )
    assert _CLAVE_DEBIL not in repr(capturados)
    assert _TOKEN not in repr(capturados)


async def test_pin_invalido_no_se_loguea(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    capturados = await _post_422(
        client, "/api/v1/auth/pin/setup", {"pin": _PIN, "pin_confirm": _PIN}, auth_headers
    )
    assert _PIN not in repr(capturados)


async def test_cambio_de_pin_invalido_no_loguea_ninguno(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    capturados = await _post_422(
        client,
        "/api/v1/auth/pin/change",
        {"current_pin": "98x6", "new_pin": _PIN, "new_pin_confirm": _PIN},
        auth_headers,
    )
    assert "98x6" not in repr(capturados)
    assert _PIN not in repr(capturados)


async def test_refresh_con_tipo_invalido_no_loguea_el_token(client: AsyncClient) -> None:
    capturados = await _post_422(client, "/api/v1/auth/refresh", {"refresh_token": [_TOKEN]})
    assert _TOKEN not in repr(capturados)


async def test_el_cliente_sigue_recibiendo_el_detalle(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/auth/login", json={"email": "no-es-un-email", "password": _CLAVE}
    )
    (error,) = resp.json()["detail"]
    assert error["loc"] == ["body", "email"]


# ── 422 interno ──────────────────────────────────────────────────────────────


async def test_un_validation_error_dentro_de_un_endpoint_no_expone_el_valor() -> None:
    """El otro handler: Pydantic validando adentro del endpoint (no el body). Su
    `input` son datos del servidor: no van al log ni a la respuesta."""
    try:
        PinVerifyRequest(pin=_PIN)
    except pydantic.ValidationError as exc:
        error = exc
    handler = cast(_Handler, app.exception_handlers[pydantic.ValidationError])
    with capture_logs() as capturados:
        resp = await handler(_request(), error)
    assert resp.status_code == 422
    assert _eventos_de_validacion(capturados)
    assert _PIN not in repr(capturados)
    assert _PIN.encode() not in bytes(resp.body)


# ── 500 ──────────────────────────────────────────────────────────────────────


async def test_un_500_por_respuesta_invalida_no_loguea_los_tokens() -> None:
    exc = ResponseValidationError(
        [
            {
                "type": "missing",
                "loc": ("response", "expires_in"),
                "msg": "Field required",
                "input": {"access_token": _TOKEN, "refresh_token": _TOKEN},
            }
        ]
    )
    with capture_logs() as capturados:
        resp = await _respuesta_500(_request(), exc)
    assert resp.status_code == 500
    assert _TOKEN not in repr(capturados)
    (evento,) = [e for e in capturados if e.get("event") == "request.unhandled_exception"]
    assert evento["exc_msg"] == [
        {"loc": ("response", "expires_in"), "msg": "Field required", "type": "missing"}
    ]


async def test_el_error_de_la_base_no_imprime_los_parametros() -> None:
    """Un token de un solo uso es la PK de su fila: si la consulta falla, su
    texto (lo que se loguea en el 500) no puede traerlo."""
    engine = crear_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.connect() as conn:
            try:
                await conn.execute(
                    text("SELECT * FROM tabla_que_no_existe WHERE id = :t"), {"t": _TOKEN}
                )
            except DBAPIError as exc:
                error = exc
    finally:
        await engine.dispose()
    assert "tabla_que_no_existe" in str(error)
    assert _TOKEN not in str(error)


# ── Sentry ───────────────────────────────────────────────────────────────────


def test_sentry_tapa_las_claves_y_el_pin_del_body() -> None:
    evento: dict[str, Any] = {
        "request": {
            "data": {
                "current_password": _CLAVE_ACTUAL,
                "new_password": _CLAVE_DEBIL,
                "new_pin": _PIN,
                "pin_confirm": _PIN,
                "refresh_token": _TOKEN,
                "items": [{"pin": _PIN}],
                "master_column_mappings": {"Envio": "shipping_cost"},
            }
        }
    }
    limpio: Any = _scrub_event(cast(Any, evento), cast(Any, {}))
    assert limpio is not None
    for secreto in (_CLAVE_ACTUAL, _CLAVE_DEBIL, _PIN, _TOKEN):
        assert secreto not in repr(limpio)
    # «pin» como palabra: `mapping`/`shipping` no se tapan.
    assert limpio["request"]["data"]["master_column_mappings"] == {"Envio": "shipping_cost"}


def test_errores_para_log_deja_solo_donde_y_por_que() -> None:
    errores: list[Any] = [
        {
            "type": "value_error",
            "loc": ("body", "password"),
            "msg": "Value error, débil",
            "input": _CLAVE,
            "ctx": {"error": ValueError(_CLAVE)},
            "url": "https://errors.pydantic.dev",
        },
        {"type": "missing", "msg": "Field required"},
        "no es un dict",
    ]
    assert errores_para_log(errores) == [
        {"loc": ("body", "password"), "msg": "Value error, débil", "type": "value_error"},
        {"msg": "Field required", "type": "missing"},
    ]
