"""El recálculo de score se encola DESPUÉS del commit, no dentro.

El worker abre su PROPIA sesión y lee la base. Si el task sale mientras la
transacción del request sigue abierta, calcula contra un estado que todavía no
existe —o que un rollback va a descartar— y persiste un score que nunca
correspondió. Con un borrado de archivo el error es visible: recalcularía con los
datos que se están revirtiendo.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

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
