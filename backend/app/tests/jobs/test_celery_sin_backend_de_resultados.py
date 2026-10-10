"""Celery corre SIN backend de resultados, aunque CELERY_RESULT_BACKEND exista.

La app nunca lee el resultado de un job. Con un backend Redis, cada
``send_task``/``.delay()`` se suscribía a él, y con Redis caído kombu reintentaba
~19 s antes de rendirse: un request que encolaba algo quedaba colgado ese tiempo,
y la suite perdía 19 s por encolado (15:49 → 6:18 al sacarlo, 2026-10-10).

Celery le da prioridad a la variable de entorno por encima de la configuración,
y Railway la tiene seteada: por eso el test la pone y exige que igual gane
``DisabledBackend``.
"""

import pytest
from celery.backends.base import DisabledBackend

from app.jobs.celery_app import celery_app


def test_el_backend_queda_deshabilitado_con_la_variable_de_entorno_seteada(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CELERY_RESULT_BACKEND", "redis://localhost:1/2")
    assert isinstance(celery_app._get_backend(), DisabledBackend)


def test_el_app_en_uso_no_tiene_backend() -> None:
    assert isinstance(celery_app.backend, DisabledBackend)
