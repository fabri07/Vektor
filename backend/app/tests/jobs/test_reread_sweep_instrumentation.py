"""E5/H16 — el barrido de relecturas deja traza de cada ejecución.

Por qué existe
--------------
``jobs.sweep_stale_reread_runs`` es el único mecanismo que recupera relecturas
colgadas sin que un usuario vuelva a tocar el archivo, y hasta acá sólo escribía
en el log. Eso hacía imposible responder desde la base la pregunta operativa que
el programa dejó pendiente: *¿Beat está publicando el barrido y hay un worker
consumiéndolo?* Un log no se puede consultar ni auditar después, y su retención
la decide la plataforma.

Lo que se afirma acá es lo que la traza tiene que cumplir para servir de
evidencia:

* se escribe **también cuando no encuentra nada** — sin la ejecución vacía, un
  Beat caído y un sistema sano se ven idénticos;
* se escribe en la **misma transacción** que el barrido, así que nunca afirma un
  trabajo que no se guardó;
* cuando el job falla, la traza dice que falló **y el error original sigue
  siendo el que ve el caller**, incluso si el propio registro falla también.

Lo que estos tests NO demuestran: que Beat lo esté publicando. Una fila en
``job_runs`` prueba que alguien ejecutó la task; que el productor sea el
scheduler y no una llamada manual es una verificación de despliegue, con
ejecuciones periódicas observadas.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.jobs import ingestion_worker, reread_sweep_worker
from app.persistence.models.job_run import JobRun


class _EngineFalso:
    """El `_run` dispone del engine al terminar; acá no hay nada que disponer."""

    async def dispose(self) -> None:
        return None


@pytest.fixture
def _sweep_contra(
    isolated_db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> async_sessionmaker[AsyncSession]:
    """Apunta el job a la base del test sin tocar la configuración global.

    El engine efímero (y no el compartido de la suite) porque estos tests
    necesitan commits REALES: la traza tiene que sobrevivir al cierre de la
    sesión del job para que la verifique otra.
    """
    factory = async_sessionmaker(isolated_db_engine, expire_on_commit=False)
    monkeypatch.setattr(
        ingestion_worker,
        "_build_async_session",
        lambda _url: (_EngineFalso(), factory),
    )
    return factory


async def _corridas(factory: async_sessionmaker[AsyncSession]) -> list[JobRun]:
    async with factory() as s:
        return list(
            (await s.execute(select(JobRun).order_by(JobRun.started_at))).scalars().all()
        )


async def test_una_corrida_sin_nada_que_barrer_igual_deja_traza(
    _sweep_contra: async_sessionmaker[AsyncSession],
) -> None:
    """El caso más frecuente y el que más importa registrar.

    Casi todas las ejecuciones del barrido no encuentran nada. Si sólo se
    registraran las que cierran algo, la tabla estaría vacía en un sistema sano y
    en uno donde el scheduler murió hace tres semanas.
    """
    resultado = await reread_sweep_worker._run()

    corridas = await _corridas(_sweep_contra)
    assert len(corridas) == 1, "una ejecución que no encuentra nada también es una ejecución"
    corrida = corridas[0]
    assert corrida.job_name == "jobs.sweep_stale_reread_runs"
    assert corrida.success is True
    assert corrida.error is None
    assert corrida.counters == dict(resultado), (
        f"los contadores no reflejan lo que hizo la corrida: {corrida.counters} vs {resultado}"
    )
    assert corrida.duration_ms >= 0


async def test_un_fallo_queda_registrado_y_el_error_original_es_el_que_sale(
    _sweep_contra: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """La traza del fallo no puede reemplazar la causa que el caller necesita ver."""
    from app.application.services import reread_service

    async def _barrido_roto(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("la base no responde")

    monkeypatch.setattr(reread_service, "sweep_stale_reread_runs", _barrido_roto)

    with pytest.raises(RuntimeError, match="la base no responde"):
        await reread_sweep_worker._run()

    corridas = await _corridas(_sweep_contra)
    assert len(corridas) == 1
    assert corridas[0].success is False
    assert "la base no responde" in (corridas[0].error or "")


async def test_si_el_registro_del_fallo_tambien_falla_gana_el_error_original(
    _sweep_contra: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """La ironía del caso: el barrido falla porque la base está caída, así que el
    registro del fallo va a fallar por lo mismo.

    Si el registro propagara, el resultado de Celery mostraría el error de una
    operación auxiliar en vez de la causa real, y el diagnóstico apuntaría al
    lugar equivocado. Se prueba contra la función real con un escritor roto, no
    mockeando el manejador entero: lo que se afirma es que su propio manejo de
    error funciona.
    """
    from app.application.services import reread_service

    async def _barrido_roto(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("la base no responde")

    async def _registro_roto(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("el registro tampoco puede escribir")

    monkeypatch.setattr(reread_service, "sweep_stale_reread_runs", _barrido_roto)
    monkeypatch.setattr(reread_sweep_worker, "record_job_run", _registro_roto)

    with pytest.raises(RuntimeError, match="la base no responde"):
        await reread_sweep_worker._run()

    assert await _corridas(_sweep_contra) == [], "no debería haber quedado traza"


async def test_la_traza_del_exito_va_en_la_misma_transaccion_que_el_barrido(
    _sweep_contra: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Si el commit falla, no puede quedar una fila afirmando un trabajo que no se
    guardó. Es la razón por la que la traza del éxito NO abre sesión propia."""
    from app.application.services import reread_service

    original = reread_service.sweep_stale_reread_runs

    async def _barre_y_despues_rompe_el_commit(session: Any, *args: Any, **kwargs: Any) -> Any:
        resultado = await original(session, *args, **kwargs)

        async def _commit_roto() -> None:
            raise RuntimeError("no se pudo commitear")

        monkeypatch.setattr(session, "commit", _commit_roto)
        return resultado

    monkeypatch.setattr(reread_service, "sweep_stale_reread_runs", _barre_y_despues_rompe_el_commit)

    with pytest.raises(RuntimeError, match="no se pudo commitear"):
        await reread_sweep_worker._run()

    corridas = await _corridas(_sweep_contra)
    exitosas = [c for c in corridas if c.success]
    assert not exitosas, f"quedó una traza de éxito sin el trabajo commiteado: {exitosas}"
