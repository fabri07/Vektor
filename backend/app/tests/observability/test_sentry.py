"""
Sentry: scrubbing, no-op con DSN vacío, y traces_sampler.

No prueba contra un proyecto Sentry real (eso es la verificación manual de
A9.2 del plan) — solo el contrato local: qué entra y qué sale de
`_scrub_event`, que `init_sentry` es fail-soft sin DSN, y que
`_traces_sampler` excluye health/OPTIONS.

`Event`/`Hint`/`SamplingContext` son `Any` en runtime (ver
`sentry_sdk/types.py`) pero TypedDicts bajo `TYPE_CHECKING` — los tests
construyen dicts planos y los pasan via `cast` para no relajar `arg-type`/
`index` (mantenidos estrictos a propósito, ver `pyproject.toml`).
"""

from typing import Any, cast

import sentry_sdk
from sentry_sdk.types import Event

from app.config.settings import get_settings
from app.observability.sentry import _scrub_event, _traces_sampler, init_sentry


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

        scrubbed = _scrub(event)

        assert scrubbed["request"]["headers"]["Authorization"] == "[Filtered]"

    def test_redacta_variable_local_por_nombre_de_clave(self) -> None:
        event = _event(
            exception={
                "values": [
                    {
                        "stacktrace": {
                            "frames": [
                                {
                                    "vars": {
                                        "monto": "15000.50",
                                        "customer_name": "Kiosco Don Pedro",
                                        "i": 3,
                                    }
                                }
                            ]
                        }
                    }
                ]
            }
        )

        scrubbed = _scrub(event)

        frame_vars = scrubbed["exception"]["values"][0]["stacktrace"]["frames"][0]["vars"]
        assert frame_vars["monto"] == "[Filtered]"
        assert frame_vars["customer_name"] == "[Filtered]"
        assert frame_vars["i"] == 3  # no sensible, no se toca

    def test_redacta_cuit_por_valor_aunque_la_clave_sea_generica(self) -> None:
        event = _event(
            exception={
                "values": [{"stacktrace": {"frames": [{"vars": {"value": "20-12345678-9"}}]}}]
            }
        )

        scrubbed = _scrub(event)

        frame_vars = scrubbed["exception"]["values"][0]["stacktrace"]["frames"][0]["vars"]
        assert frame_vars["value"] == "[Filtered]"

    def test_redacta_breadcrumbs_y_extra(self) -> None:
        event = _event(
            breadcrumbs={"values": [{"data": {"email": "cliente@example.com"}}]},
            extra={"dni": "30111222"},
        )

        scrubbed = _scrub(event)

        assert scrubbed["breadcrumbs"]["values"][0]["data"]["email"] == "[Filtered]"
        assert scrubbed["extra"]["dni"] == "[Filtered]"

    def test_no_toca_datos_no_sensibles(self) -> None:
        event = _event(extra={"endpoint": "/sales", "status_code": 500})

        scrubbed = _scrub(event)

        assert scrubbed["extra"] == {"endpoint": "/sales", "status_code": 500}


class TestInitSentryDsnVacio:
    def test_no_inicializa_sin_dsn(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(get_settings(), "SENTRY_DSN", "")

        init_sentry("web")

        assert sentry_sdk.is_initialized() is False


class TestTracesSampler:
    def test_excluye_health_por_transaction_name(self) -> None:
        rate = _traces_sampler(cast(Any, {"transaction_context": {"name": "GET /health"}}))
        assert rate == 0.0

    def test_excluye_ready_por_transaction_name(self) -> None:
        rate = _traces_sampler(cast(Any, {"transaction_context": {"name": "GET /ready"}}))
        assert rate == 0.0

    def test_excluye_options_por_asgi_scope(self) -> None:
        rate = _traces_sampler(
            cast(
                Any,
                {
                    "transaction_context": {"name": "OPTIONS /api/v1/sales"},
                    "asgi_scope": {"method": "OPTIONS", "path": "/api/v1/sales"},
                },
            )
        )
        assert rate == 0.0

    def test_usa_el_sample_rate_configurado_para_el_resto(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(get_settings(), "SENTRY_TRACES_SAMPLE_RATE", 0.1)

        rate = _traces_sampler(cast(Any, {"transaction_context": {"name": "GET /api/v1/sales"}}))

        assert rate == 0.1

    def test_no_excluye_rutas_de_negocio_que_contienen_health_como_substring(
        self, monkeypatch: Any
    ) -> None:
        """Regresión: '/health' in name matcheaba /health-scores y /settings/health-config."""
        monkeypatch.setattr(get_settings(), "SENTRY_TRACES_SAMPLE_RATE", 0.1)

        rate_health_scores = _traces_sampler(
            cast(Any, {"transaction_context": {"name": "GET /api/v1/health-scores/history/v2"}})
        )
        rate_health_config = _traces_sampler(
            cast(Any, {"transaction_context": {"name": "PATCH /api/v1/settings/health-config"}})
        )

        assert rate_health_scores == 0.1
        assert rate_health_config == 0.1
