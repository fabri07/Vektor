"""
Sentry (MCP server): scrubbing, no-op con DSN vacío y traces_sampler.

Espeja `backend/app/tests/observability/test_sentry.py`, con los casos que este
servicio necesita de más: credenciales de Google OAuth (access/refresh token,
client_secret, id_token JWT) y el `?code=...&state=...` del callback.

No prueba contra un proyecto Sentry real — solo el contrato local.
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Any, cast

import pytest
import sentry_sdk
from sentry_sdk.types import Event

from app.config import get_settings
from app.observability.sentry import _scrub_event, _traces_sampler, init_sentry


@pytest.fixture(autouse=True)
def settings_env(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/vektor")
    monkeypatch.setenv("GOOGLE_MCP_OAUTH_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_MCP_OAUTH_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("GOOGLE_MCP_OAUTH_REDIRECT_URI", "http://localhost:8080/auth/callback")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _event(**overrides: Any) -> Event:
    base: dict[str, Any] = {
        "request": {},
        "exception": {"values": []},
        "breadcrumbs": {"values": []},
        "extra": {},
    }
    base.update(overrides)
    return cast(Event, base)


def _scrub(event: Event) -> dict[str, Any]:
    scrubbed = _scrub_event(event, cast(Any, {}))
    assert scrubbed is not None
    return cast(dict[str, Any], scrubbed)


class TestScrubEvent:
    def test_redacta_header_authorization(self) -> None:
        event = _event(request={"headers": {"Authorization": "Bearer secreto123"}})

        assert _scrub(event)["request"]["headers"]["Authorization"] == "[Filtered]"

    def test_descarta_la_query_del_callback_de_oauth(self) -> None:
        # `?code=` es el código de autorización de Google: canjeable por tokens.
        event = _event(request={"query_string": "code=4/0AX4Xf&state=abc"})

        assert _scrub(event)["request"]["query_string"] == "[Filtered]"

    def test_redacta_tokens_de_google_por_nombre_de_clave(self) -> None:
        event = _event(
            extra={
                "access_token": "ya29.a0ARr",
                "refresh_token": "1//04xyz",
                "client_secret": "GOCSPX-abc",
                "tenant_id": "8f3c",
            }
        )

        scrubbed = _scrub(event)["extra"]

        assert scrubbed["access_token"] == "[Filtered]"
        assert scrubbed["refresh_token"] == "[Filtered]"
        assert scrubbed["client_secret"] == "[Filtered]"
        # Lo que no es sensible sigue viajando: sin esto el evento no sirve.
        assert scrubbed["tenant_id"] == "8f3c"

    def test_redacta_token_de_google_bajo_una_clave_generica(self) -> None:
        # El filtro por NOMBRE no ve esto; lo agarra el filtro por VALOR.
        event = _event(extra={"payload": "ya29.a0ARrdaM-token-largo"})

        assert _scrub(event)["extra"]["payload"] == "[Filtered]"

    def test_redacta_jwt_bajo_una_clave_generica(self) -> None:
        jwt = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjMifQ.firma-falsa"
        event = _event(extra={"value": jwt})

        assert _scrub(event)["extra"]["value"] == "[Filtered]"

    def test_redacta_email_en_variables_locales_del_stacktrace(self) -> None:
        event = _event(
            exception={
                "values": [
                    {
                        "stacktrace": {
                            "frames": [
                                {"vars": {"email": "dueño@kiosco.com", "intentos": 2}}
                            ]
                        }
                    }
                ]
            }
        )

        frame_vars = _scrub(event)["exception"]["values"][0]["stacktrace"]["frames"][0]["vars"]

        assert frame_vars["email"] == "[Filtered]"
        assert frame_vars["intentos"] == 2

    def test_redacta_data_de_breadcrumbs(self) -> None:
        event = _event(breadcrumbs={"values": [{"data": {"id_token": "eyJ0"}}]})

        assert _scrub(event)["breadcrumbs"]["values"][0]["data"]["id_token"] == "[Filtered]"


class TestInitSentry:
    def test_es_no_op_sin_dsn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SENTRY_DSN", raising=False)
        get_settings.cache_clear()

        init_sentry()  # no debe levantar

        assert not sentry_sdk.is_initialized()


class TestTracesSampler:
    def test_excluye_health_check(self) -> None:
        ctx = cast(Any, {"asgi_scope": {"method": "GET", "path": "/health"}})

        assert _traces_sampler(ctx) == 0.0

    def test_excluye_preflight_cors(self) -> None:
        ctx = cast(Any, {"asgi_scope": {"method": "OPTIONS", "path": "/tools/gmail"}})

        assert _traces_sampler(ctx) == 0.0

    def test_no_apaga_rutas_que_comparten_substring(self) -> None:
        # "/health" como SUBSTRING mataba el sampling de rutas reales.
        ctx = cast(Any, {"asgi_scope": {"method": "GET", "path": "/auth/health-check-status"}})

        assert _traces_sampler(ctx) == get_settings().SENTRY_TRACES_SAMPLE_RATE

    def test_usa_el_sample_rate_configurado(self) -> None:
        ctx = cast(Any, {"transaction_context": {"name": "GET /tools/drive"}})

        assert _traces_sampler(ctx) == get_settings().SENTRY_TRACES_SAMPLE_RATE
