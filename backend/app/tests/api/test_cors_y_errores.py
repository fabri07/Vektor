"""CORS de los headers propios y 500 legible por el navegador.

Lo que estos tests fijan lo mira el NAVEGADOR, no el servidor: un header propio
que falta en `allow_headers` hace que el preflight conteste 400 y el request
nunca salga; un 500 sin `Access-Control-Allow-Origin` se bloquea y axios lo ve
como un error sin respuesta. En los dos casos la caja decía "Sin conexión" con
la red andando (incidente de 2026-10: ninguna venta encolada podía subir).
"""

import pathlib
import re
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.config.settings import get_settings
from app.domain.pos_terminal import OFFLINE_HEADER, TERMINAL_HEADER
from app.main import CORS_ALLOW_HEADERS, create_app

ORIGEN_AJENO = "https://no-es-vektor.example"


def _origen_permitido() -> str:
    return get_settings().CORS_ORIGINS[0]


@pytest_asyncio.fixture
async def cliente() -> AsyncGenerator[AsyncClient, None]:
    app = create_app()

    async def explota() -> None:
        raise RuntimeError("falla de prueba")

    app.add_api_route("/api/v1/_prueba/explota", explota, methods=["POST"])
    # `raise_app_exceptions=False`: si el 500 escapara hasta `ServerErrorMiddleware`
    # (el defecto que se cubre acá), el test tiene que ver la respuesta sin CORS,
    # no una excepción del transporte.
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as ac:
        yield ac


def _preflight_headers(origen: str, pedidos: str) -> dict[str, str]:
    return {
        "Origin": origen,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": pedidos,
    }


class TestPreflight:
    @pytest.mark.parametrize(
        "header", [OFFLINE_HEADER, TERMINAL_HEADER, "X-Trace-Id", "Idempotency-Key"]
    )
    async def test_los_headers_propios_pasan_el_preflight(
        self, cliente: AsyncClient, header: str
    ) -> None:
        resp = await cliente.options(
            "/api/v1/pos/operations",
            headers=_preflight_headers(
                _origen_permitido(), f"authorization,content-type,{header.lower()}"
            ),
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers["access-control-allow-origin"] == _origen_permitido()
        permitidos = {
            h.strip().lower() for h in resp.headers["access-control-allow-headers"].split(",")
        }
        assert header.lower() in permitidos

    async def test_un_origen_ajeno_no_recibe_permiso(self, cliente: AsyncClient) -> None:
        resp = await cliente.options(
            "/api/v1/pos/operations",
            headers=_preflight_headers(ORIGEN_AJENO, OFFLINE_HEADER.lower()),
        )
        assert resp.status_code == 400
        assert "access-control-allow-origin" not in resp.headers


class TestErrorNoManejado:
    async def test_el_500_lleva_cors_y_trace_id(self, cliente: AsyncClient) -> None:
        resp = await cliente.post(
            "/api/v1/_prueba/explota",
            headers={"Origin": _origen_permitido(), "X-Trace-Id": "traza-de-prueba"},
        )
        assert resp.status_code == 500
        assert resp.headers["access-control-allow-origin"] == _origen_permitido()
        # El trace id llega y el navegador lo puede leer: es lo que la caja
        # muestra para cruzar con Sentry.
        assert resp.headers["x-trace-id"] == "traza-de-prueba"
        expuestos = {
            h.strip().lower()
            for h in resp.headers["access-control-expose-headers"].split(",")
        }
        assert "x-trace-id" in expuestos
        # Sin detalles internos fuera de desarrollo.
        assert "detail" in resp.json()

    async def test_el_500_no_da_permiso_a_un_origen_ajeno(self, cliente: AsyncClient) -> None:
        resp = await cliente.post(
            "/api/v1/_prueba/explota", headers={"Origin": ORIGEN_AJENO}
        )
        assert resp.status_code == 500
        assert "access-control-allow-origin" not in resp.headers


# ── El front no puede mandar un header que el preflight rechace ──────────────

_FRONT = pathlib.Path(__file__).resolve().parents[4] / "frontend" / "src"
#: Un literal con forma de header (``"Idempotency-Key":`` o ``headers["X-Trace-Id"]``).
_LITERAL_DE_HEADER = re.compile(r"""["']([A-Z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)+)["']\s*(?::|\])""")


def _headers_que_manda_el_front() -> dict[str, str]:
    encontrados: dict[str, str] = {}
    for archivo in _FRONT.rglob("*"):
        if archivo.suffix not in (".ts", ".tsx") or "__tests__" in archivo.parts:
            continue
        texto = archivo.read_text(encoding="utf-8")
        if "headers" not in texto.lower():
            continue
        for m in _LITERAL_DE_HEADER.finditer(texto):
            encontrados.setdefault(m.group(1), str(archivo.relative_to(_FRONT)))
    return encontrados


@pytest.mark.skipif(not _FRONT.is_dir(), reason="sin el código del frontend al lado")
def test_todo_header_propio_del_front_esta_permitido() -> None:
    """Compuerta: un header nuevo en el front que no está en ``CORS_ALLOW_HEADERS``
    hace que el navegador corte el preflight y la operación quede «sin conexión»
    para siempre. Pasó dos veces (``X-POS-*`` hasta #82 e ``Idempotency-Key`` hasta
    2026-10-11): se lee del código del front en vez de confiar en la memoria."""
    encontrados = _headers_que_manda_el_front()
    # Sanidad: si el patrón deja de encontrar los conocidos, la compuerta no prueba nada.
    assert {"Idempotency-Key", "X-Trace-Id"} <= set(encontrados), encontrados
    permitidos = {h.lower() for h in CORS_ALLOW_HEADERS}
    faltan = {h: f for h, f in encontrados.items() if h.lower() not in permitidos}
    assert not faltan, f"headers del front fuera de CORS_ALLOW_HEADERS: {faltan}"
