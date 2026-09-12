"""E3 (H06) — qué se reintenta y qué no.

`max_retries=3` estaba declarado en las tres tasks de ingestión desde siempre,
pero sin `bind=True`, sin `self.retry()` y sin `autoretry_for`: no reintentaban
nunca. `task_acks_late=True` da re-entrega si el worker MUERE, que es otra cosa
— ante una excepción, el archivo iba directo a FAILED.

Al encenderlo, la pregunta que importa no es "cuántas veces" sino "qué": un
archivo con una columna rota va a fallar igual las tres veces, y reintentarlo
sólo retrasa el diagnóstico y ocupa el worker. Por eso **el default es no
reintentar** y la lista de transitorios es explícita.
"""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.jobs.ingestion_worker import _es_transitorio, _espera_antes_de_reintentar


def _client_error(code: str, status: int | None) -> ClientError:
    respuesta: Any = {"Error": {"Code": code, "Message": code}}
    if status is not None:
        respuesta["ResponseMetadata"] = {"HTTPStatusCode": status}
    # `ClientError` tipa su respuesta con TypedDicts completos de botocore; acá
    # sólo interesan los dos campos que lee `_es_transitorio`.
    return ClientError(respuesta, "GetObject")


class TestLoQueSeReintenta:
    def test_corte_de_conexion_con_s3(self) -> None:
        """`BotoCoreError` es la familia de timeouts, DNS y conexión: no llegó a
        haber respuesta HTTP, así que no dice nada del objeto."""
        assert _es_transitorio(EndpointConnectionError(endpoint_url="https://r2")) is True

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_error_del_lado_del_servicio(self, status: int) -> None:
        assert _es_transitorio(_client_error("InternalError", status)) is True

    def test_throttling(self) -> None:
        assert _es_transitorio(_client_error("SlowDown", 429)) is True

    def test_client_error_sin_status(self) -> None:
        """Sin metadata de respuesta no se puede afirmar que el objeto esté mal."""
        assert _es_transitorio(_client_error("Unknown", None)) is True

    def test_conexion_de_base_invalidada(self) -> None:
        """Neon cierra conexiones ociosas: el siguiente intento abre una nueva."""
        exc = DBAPIError("SELECT 1", {}, Exception("server closed the connection"))
        exc.connection_invalidated = True
        assert _es_transitorio(exc) is True

    def test_timeouts_de_python(self) -> None:
        assert _es_transitorio(TimeoutError("timed out")) is True
        assert _es_transitorio(ConnectionError("reset by peer")) is True


class TestLoQueNoSeReintenta:
    @pytest.mark.parametrize(
        "code", ["NoSuchKey", "NoSuchBucket", "AccessDenied", "InvalidAccessKeyId"]
    )
    def test_el_objeto_no_esta_o_no_hay_permiso(self, code: str) -> None:
        """404 y 403 no mejoran con el tiempo. Reintentar sólo retrasa el FAILED.

        Se clasifica por CÓDIGO y no por status: un `AccessDenied` puede venir con
        403 o sin metadata, y en los dos casos la respuesta es la misma.
        """
        assert _es_transitorio(_client_error(code, 404)) is False

    def test_error_de_parseo(self) -> None:
        """El archivo está roto: las tres veces va a estar igual de roto."""
        assert _es_transitorio(ValueError("no se pudo leer la hoja")) is False

    def test_error_de_integridad(self) -> None:
        """Un dato que viola una restricción no se arregla esperando."""
        exc = IntegrityError("INSERT", {}, Exception("duplicate key"))
        assert _es_transitorio(exc) is False

    def test_una_excepcion_cualquiera(self) -> None:
        """El default: si no se sabe que es transitorio, NO se reintenta."""
        assert _es_transitorio(Exception("algo pasó")) is False
        assert _es_transitorio(KeyError("columna")) is False


class TestLaEsperaCrece:
    def test_backoff_exponencial(self) -> None:
        """Un servicio caído no se recupera en tres segundos, y machacarlo cada
        30 s empeora una caída por saturación."""
        assert [_espera_antes_de_reintentar(i) for i in range(3)] == [30, 60, 120]
