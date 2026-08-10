"""``EventBus.emit_after_commit``: el evento se difiere hasta el commit del request.

Regresión del backstop de descuento de stock: emitir SALE_RECORDED de forma síncrona
permitía que el task async corriera ANTES del commit, no encontrara el SaleEntry y
terminara sin reintentar. Debe diferirse al ``after_commit`` de la sesión.
"""

from __future__ import annotations

import unittest.mock

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agents.shared.event_bus import EventBus


async def test_emit_after_commit_does_not_emit_immediately(
    db_session: AsyncSession,
) -> None:
    with unittest.mock.patch(
        "app.application.agents.shared.event_bus.EventBus.emit"
    ) as mock_emit:
        EventBus.emit_after_commit(db_session, "SALE_RECORDED", {"sale_id": "s1"})
        # Registrado como listener de after_commit — NO emitido todavía.
        mock_emit.assert_not_called()


async def test_emit_after_commit_fires_on_commit(db_session: AsyncSession) -> None:
    """Al commitear la sesión, el listener dispara EventBus.emit exactamente una vez."""
    with unittest.mock.patch(
        "app.application.agents.shared.event_bus.EventBus.emit"
    ) as mock_emit:
        EventBus.emit_after_commit(db_session, "SALE_RECORDED", {"sale_id": "s1"})
        # Disparar el evento after_commit directamente sobre la sync session (sin
        # depender de si el fixture de tests hace un commit real vs. savepoint).
        db_session.sync_session.dispatch.after_commit(db_session.sync_session)
        mock_emit.assert_called_once_with("SALE_RECORDED", {"sale_id": "s1"})
