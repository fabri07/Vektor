"""Una sola noción de "la IP del cliente" (deps.client_ip / deps.rate_limit_key).

Antes convivían dos DENTRO del mismo handler: el `ip_hash` de
`POST /access-requests` leía `X-Forwarded-For` y su `@limiter.limit("5/hour")`
usaba `get_remote_address`, que lo ignora. Detrás del edge de Railway eso podía
volver el 5/hour un techo GLOBAL del único embudo de alta.

La confianza en el header está atada al DESPLIEGUE, no al header: `X-Real-IP`
solo se cree en producción, porque cualquiera que alcance uvicorn directo puede
inventarlo.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.requests import Request
from structlog.testing import capture_logs

from app.api.v1.deps import _SIN_IP, client_ip, rate_limit_key

_IP_EDGE = "203.0.113.7"
_IP_PEER = "10.0.0.5"


def _request(*, headers: dict[str, str] | None = None, peer: str | None) -> Request:
    scope: dict[str, object] = {
        "type": "http",
        "method": "POST",
        "path": "/access-requests",
        "headers": [
            (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()
        ],
        "client": (peer, 12345) if peer is not None else None,
    }
    return Request(scope)


@pytest.fixture
def entorno(monkeypatch: pytest.MonkeyPatch):
    """Fija `is_production` sin tocar el `lru_cache` global de `get_settings`."""

    def _fijar(*, produccion: bool) -> None:
        monkeypatch.setattr(
            "app.api.v1.deps.get_settings",
            lambda: SimpleNamespace(is_production=produccion),
        )

    return _fijar


def test_en_produccion_confia_en_x_real_ip(entorno) -> None:
    entorno(produccion=True)
    request = _request(headers={"X-Real-IP": f" {_IP_EDGE} "}, peer=_IP_PEER)

    assert client_ip(request) == _IP_EDGE


def test_fuera_de_produccion_ignora_el_header(entorno) -> None:
    """En dev/tests el header es inventable por cualquiera que alcance uvicorn."""
    entorno(produccion=False)
    request = _request(headers={"X-Real-IP": _IP_EDGE}, peer=_IP_PEER)

    assert client_ip(request) == _IP_PEER


def test_sin_header_degrada_al_peer(entorno) -> None:
    """Comportamiento previo (`get_remote_address`): si el edge no manda el
    header, esto no empeora nada."""
    entorno(produccion=True)
    request = _request(peer=_IP_PEER)

    assert client_ip(request) == _IP_PEER


@pytest.mark.parametrize("produccion", [True, False])
def test_x_forwarded_for_ya_no_se_lee(entorno, produccion: bool) -> None:
    """La cadena de `X-Forwarded-For` puede traer varias IPs y su primer valor no
    se puede creer sin una política de proxies confiables."""
    entorno(produccion=produccion)
    request = _request(
        headers={"X-Forwarded-For": f"{_IP_EDGE}, 198.51.100.1"}, peer=_IP_PEER
    )

    assert client_ip(request) == _IP_PEER


def test_sin_peer_es_none_y_no_un_valor_inventado(entorno) -> None:
    """`None` es "no la sé": `hash_ip(None)` es `None`, así que no se guarda un
    `ip_hash` de un literal indistinguible de una IP real."""
    entorno(produccion=True)
    request = _request(peer=None)

    assert client_ip(request) is None


def test_la_key_del_limiter_es_la_misma_ip_que_el_ip_hash(entorno) -> None:
    """El punto entero del arreglo: una sola resolución para los dos usos."""
    entorno(produccion=True)
    request = _request(headers={"X-Real-IP": _IP_EDGE}, peer=_IP_PEER)

    assert rate_limit_key(request) == client_ip(request) == _IP_EDGE


def test_la_key_del_limiter_colapsa_el_desconocido_a_un_centinela(entorno) -> None:
    """`slowapi` exige un `str`: sin IP todos comparten cubeta (conservador)."""
    entorno(produccion=True)
    request = _request(peer=None)

    assert rate_limit_key(request) == _SIN_IP


def test_el_limiter_global_usa_esta_key() -> None:
    """Sin esto, el arreglo podría estar completo en `deps` y no cableado."""
    from app.main import limiter

    assert limiter._key_func is rate_limit_key


# ── La suposición sobre X-Real-IP se autodenuncia ─────────────────────────────


@pytest.fixture
def aviso_rearmado(monkeypatch: pytest.MonkeyPatch) -> None:
    """Devuelve el flag de "ya avisé" a su estado inicial para cada test.

    Es global por proceso a propósito (el aviso es de configuración del deploy,
    no por request), así que sin esto el orden de los tests decidiría el
    resultado.
    """
    monkeypatch.setattr("app.api.v1.deps._aviso_sin_x_real_ip_emitido", False)


def _eventos(capturados: Sequence[Mapping[str, Any]]) -> list[str]:
    return [str(e.get("event")) for e in capturados]


def test_en_produccion_sin_x_real_ip_se_avisa_una_sola_vez(
    entorno, aviso_rearmado: None
) -> None:
    """Si el edge no manda el header, el `ip_hash` deja de identificar visitantes
    y pasa a guardar el hash del proxy para todos — un valor que PARECE un dato.
    El aviso existe para que eso se note en los logs y no dependa de que alguien
    se acuerde de ir a mirar."""
    entorno(produccion=True)

    with capture_logs() as capturados:
        assert client_ip(_request(peer=_IP_PEER)) == _IP_PEER
        assert client_ip(_request(peer=_IP_PEER)) == _IP_PEER

    assert _eventos(capturados).count("client_ip.sin_x_real_ip") == 1


def test_el_aviso_no_filtra_la_ip(entorno, aviso_rearmado: None) -> None:
    """El `ip_hash` nunca se imprime, y la IP cruda menos todavía."""
    entorno(produccion=True)

    with capture_logs() as capturados:
        client_ip(_request(peer=_IP_PEER))

    assert _IP_PEER not in repr(capturados)


def test_con_x_real_ip_no_se_avisa(entorno, aviso_rearmado: None) -> None:
    """Control positivo: sin esto el test de arriba pasaría con un aviso que se
    emite siempre, incluso cuando el header llega bien."""
    entorno(produccion=True)

    with capture_logs() as capturados:
        assert client_ip(_request(headers={"X-Real-IP": _IP_EDGE}, peer=_IP_PEER)) == _IP_EDGE

    assert "client_ip.sin_x_real_ip" not in _eventos(capturados)


def test_fuera_de_produccion_no_se_avisa(entorno, aviso_rearmado: None) -> None:
    """En dev y tests el header no se lee a propósito, así que su ausencia no es
    una anomalía que reportar."""
    entorno(produccion=False)

    with capture_logs() as capturados:
        client_ip(_request(peer=_IP_PEER))

    assert "client_ip.sin_x_real_ip" not in _eventos(capturados)
