"""El recálculo de score se encola DESPUÉS del commit, no dentro.

El worker abre su PROPIA sesión y lee la base. Si el task sale mientras la
transacción del request sigue abierta, calcula contra un estado que todavía no
existe —o que un rollback va a descartar— y persiste un score que nunca
correspondió. Con un borrado de archivo el error es visible: recalcularía con los
datos que se están revirtiendo.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from unittest.mock import MagicMock, patch

import pytest
from fastapi import BackgroundTasks as FastAPIBackgroundTasks
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTasks

from app.application.services.score_trigger_service import (
    trigger_score_recalculation_after_commit,
)
from app.persistence.models.tenant import Tenant

pytestmark = pytest.mark.asyncio


async def test_no_encola_antes_del_commit(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    with patch(
        "app.application.services.score_trigger_service.trigger_score_recalculation"
    ) as task:
        task.delay = MagicMock()
        trigger_score_recalculation_after_commit(
            db_session, str(sample_tenant.tenant_id), "file_deleted"
        )
        # Registrado, pero todavía NO disparado: falta el commit.
        task.delay.assert_not_called()

        await db_session.commit()
        task.delay.assert_called_once_with(str(sample_tenant.tenant_id), "file_deleted")


async def test_un_rollback_no_encola_nada(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Si la transacción se descarta, el score nunca se recalcula: no pasó nada."""
    with patch(
        "app.application.services.score_trigger_service.trigger_score_recalculation"
    ) as task:
        task.delay = MagicMock()
        trigger_score_recalculation_after_commit(
            db_session, str(sample_tenant.tenant_id), "file_deleted"
        )
        await db_session.rollback()
        task.delay.assert_not_called()


async def test_con_background_el_commit_agenda_pero_no_habla_con_el_broker(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Con ``BackgroundTasks``, el ``.delay()`` sale del camino de la respuesta.

    Sin esto, la llamada al broker corre dentro del request (en el commit de
    ``get_db_session``) y un broker lento o caído lo paga el usuario esperando
    por una operación que ya terminó.
    """
    background = BackgroundTasks()
    with patch(
        "app.application.services.score_trigger_service.trigger_score_recalculation"
    ) as task:
        task.delay = MagicMock()
        trigger_score_recalculation_after_commit(
            db_session, str(sample_tenant.tenant_id), "file_deleted", background=background
        )
        await db_session.commit()
        # El commit sólo AGENDÓ: todavía no se habló con el broker.
        assert len(background.tasks) == 1
        task.delay.assert_not_called()

        # Starlette las corre recién después de enviar la respuesta.
        await background()
        task.delay.assert_called_once_with(str(sample_tenant.tenant_id), "file_deleted")


async def test_con_background_un_rollback_no_agenda_nada(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """La compuerta sigue siendo el commit: agendar en el registro encolaría el
    recálculo de una operación que se descartó."""
    background = BackgroundTasks()
    with patch(
        "app.application.services.score_trigger_service.trigger_score_recalculation"
    ) as task:
        task.delay = MagicMock()
        trigger_score_recalculation_after_commit(
            db_session, str(sample_tenant.tenant_id), "file_deleted", background=background
        )
        await db_session.rollback()
        assert background.tasks == []

        await background()
        task.delay.assert_not_called()


async def test_con_background_un_broker_caido_no_rompe_nada(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """El fail-safe vale igual como background task: la respuesta ya salió, así
    que propagar sólo ensuciaría el log con un error que no es del usuario."""
    background = BackgroundTasks()
    with patch(
        "app.application.services.score_trigger_service.trigger_score_recalculation"
    ) as task:
        task.delay = MagicMock(side_effect=ConnectionError("redis caído"))
        trigger_score_recalculation_after_commit(
            db_session, str(sample_tenant.tenant_id), "file_deleted", background=background
        )
        await db_session.commit()
        await background()  # no debe levantar
        task.delay.assert_called_once()


async def test_un_broker_caido_no_rompe_la_respuesta(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Acá la transacción YA commiteó: propagar la excepción le devolvería un 500
    al usuario por una operación que sí se completó."""
    with patch(
        "app.application.services.score_trigger_service.trigger_score_recalculation"
    ) as task:
        task.delay = MagicMock(side_effect=ConnectionError("redis caído"))
        trigger_score_recalculation_after_commit(
            db_session, str(sample_tenant.tenant_id), "file_deleted"
        )
        await db_session.commit()  # no debe levantar
        task.delay.assert_called_once()


async def test_el_commit_de_la_dependency_llega_a_tiempo_de_agendar() -> None:
    """Pinea la garantía de FastAPI de la que depende sacar el encolado del request.

    El encolado se agenda en el ``after_commit`` de la sesión, y esa sesión la
    commitea ``get_db_session`` en el teardown de la dependency. Si ese teardown
    corriera DESPUÉS de las background tasks, el ``add_task`` caería sobre unas
    ``BackgroundTasks`` ya ejecutadas y el score no se recalcularía nunca —en
    silencio, que es la peor forma de fallar—. FastAPI ≥0.106 garantiza el orden
    contrario (las dependencias con ``yield`` cierran antes de enviar la
    respuesta, y las tasks corren después); este test lo verifica en vez de
    confiar en el changelog, para que un upgrade que lo cambie falle acá.

    No usa el `client` de la suite a propósito: ese fixture pisa
    ``get_db_session`` por una sesión que NO comitea, así que ningún
    ``after_commit`` corre dentro de un request en los tests.
    """
    orden: list[str] = []
    app = FastAPI()

    async def sesion_falsa(
        background: FastAPIBackgroundTasks,
    ) -> AsyncGenerator[None, None]:
        yield
        # Teardown = el commit de `get_db_session`; el listener `after_commit`
        # agenda ahí el encolado.
        orden.append("commit")
        background.add_task(lambda: orden.append("encolado"))

    @app.post("/x")
    async def _endpoint(_: None = Depends(sesion_falsa)) -> dict[str, bool]:
        orden.append("handler")
        return {"ok": True}

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        assert (await ac.post("/x")).status_code == 200

    assert orden == ["handler", "commit", "encolado"]
