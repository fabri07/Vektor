"""UPDATE_PRODUCT desde el chat: el recálculo del score se agenda sin romper.

Hasta 2026-10-09 el camino que cambia precio, costo o stock importaba
``app.jobs.score_trigger``, un módulo que no existe: la acción aprobada
reventaba con ``ModuleNotFoundError`` DESPUÉS de ``repo.save``. Ningún test
recorría ese camino (los que ejecutan UPDATE_PRODUCT cambian otros campos o se
cortan antes, en el gate de suscripción).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agents.shared.schemas import ActionType
from app.application.services import score_trigger_service
from app.application.services.pending_action_service import execute_pending_action
from app.persistence.models.pending_action import PendingAction
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant


def _accion(tenant_id: uuid.UUID, payload: dict[str, object]) -> PendingAction:
    action = PendingAction()
    action.id = uuid.uuid4()
    action.tenant_id = tenant_id
    action.user_id = uuid.uuid4()
    action.action_type = ActionType.UPDATE_PRODUCT
    action.payload = payload
    action.risk_level = "MEDIUM"
    action.status = "APPROVED"
    action.external_system = None
    return action


async def test_cambiar_el_precio_guarda_y_agenda_el_recalculo(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agendados: list[tuple[str, str]] = []

    def _agendar(db: AsyncSession, tenant_id: str, triggered_by: str, *a: Any, **k: Any) -> None:
        agendados.append((tenant_id, triggered_by))

    monkeypatch.setattr(
        score_trigger_service, "trigger_score_recalculation_after_commit", _agendar
    )

    producto = Product(
        id=uuid.uuid4(),
        tenant_id=sample_tenant.tenant_id,
        name="Coca 500ml",
        sale_price_ars=Decimal("1200"),
        unit_cost_ars=Decimal("700"),
        stock_units=10,
    )
    db_session.add(producto)
    await db_session.flush()

    await execute_pending_action(
        _accion(
            sample_tenant.tenant_id,
            {"product_id": str(producto.id), "sale_price_ars": "1500"},
        ),
        db_session,
    )

    await db_session.refresh(producto)
    assert producto.sale_price_ars == Decimal("1500")
    assert agendados == [(str(sample_tenant.tenant_id), "product_updated")]
