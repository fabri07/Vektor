"""T3 (E0-B-2, C-5): la merma aprobada por chat no puede superar el stock.

Camino real: una ``PendingAction`` REGISTER_STOCK_LOSS confirmada por
``POST /agent/confirm/{id}`` → ``execute_pending_action`` →
``stock_service.register_stock_loss``. Antes, una merma de 10 sobre un stock de
3 dejaba ``stock_units`` en 0 (recortado), el balance en −7 y el movimiento
``loss`` y la auditoría en 10. Ahora se rechaza antes de escribir nada.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agents.shared.event_bus import EventBus
from app.main import create_app
from app.persistence.db.redis_client import get_redis
from app.persistence.db.session import get_db_session
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.inventory import InventoryBalance, InventoryMovement
from app.persistence.models.pending_action import PendingAction
from app.persistence.models.product import Product

from .conftest import FakeRedis, make_auth_headers, make_tenant, make_user

STOCK_INICIAL = 3


@pytest_asyncio.fixture
async def escenario(session: AsyncSession) -> Any:
    tenant = await make_tenant(session)
    user = await make_user(session, tenant.tenant_id)
    producto = Product(
        tenant_id=tenant.tenant_id,
        name="Yogur bebible",
        sale_price_ars=Decimal("900.00"),
        unit_cost_ars=Decimal("500.00"),
        stock_units=STOCK_INICIAL,
        low_stock_threshold_units=1,
        is_active=True,
    )
    session.add(producto)
    await session.flush()
    session.add(
        InventoryBalance(
            tenant_id=tenant.tenant_id,
            product_id=producto.id,
            current_qty=STOCK_INICIAL,
            reserved_qty=0,
        )
    )
    await session.commit()

    app = create_app()

    async def _override_session():
        yield session

    async def _override_redis():
        yield FakeRedis()

    app.dependency_overrides[get_db_session] = _override_session
    app.dependency_overrides[get_redis] = _override_redis

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac, make_auth_headers(user.user_id, tenant.tenant_id), tenant, user, producto


async def _confirmar_merma(
    session: AsyncSession, escenario: Any, qty: int
) -> tuple[dict[str, Any], PendingAction]:
    ac, headers, tenant, user, producto = escenario
    action = PendingAction(
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        action_type="REGISTER_STOCK_LOSS",
        payload={"product_id": str(producto.id), "qty_change": -qty, "reason": "se rompieron"},
        risk_level="HIGH",
        status="PENDING",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    session.add(action)
    await session.commit()

    with patch.object(EventBus, "emit"):
        resp = await ac.post(f"/api/v1/agent/confirm/{action.id}", headers=headers)
    assert resp.status_code == 200, resp.text
    await session.refresh(action)
    return resp.json(), action


async def _estado(session: AsyncSession, escenario: Any) -> tuple[int, int, list[int], int]:
    _, _, tenant, _, producto = escenario
    producto_id, tenant_id = producto.id, tenant.tenant_id
    # Lo que quedó en la base, no lo que tiene la identity map.
    session.expire_all()
    stock = (
        await session.execute(select(Product.stock_units).where(Product.id == producto_id))
    ).scalar_one()
    balance = (
        await session.execute(
            select(InventoryBalance.current_qty).where(InventoryBalance.product_id == producto_id)
        )
    ).scalar_one()
    mermas = list(
        (
            await session.execute(
                select(InventoryMovement.qty).where(
                    InventoryMovement.product_id == producto_id,
                    InventoryMovement.movement_type == "loss",
                )
            )
        )
        .scalars()
        .all()
    )
    auditorias = len(
        (
            await session.execute(
                select(DecisionAuditLog.id).where(
                    DecisionAuditLog.tenant_id == tenant_id,
                    DecisionAuditLog.decision_type == "STOCK_LOSS",
                )
            )
        )
        .scalars()
        .all()
    )
    return stock, balance, mermas, auditorias


async def test_merma_mayor_que_el_stock_se_rechaza_y_no_escribe_nada(
    session: AsyncSession, escenario: Any
) -> None:
    data, action = await _confirmar_merma(session, escenario, qty=10)

    assert data["execution_status"] == "FAILED"
    assert action.execution_status == "FAILED"
    # El chat muestra el motivo con el producto, el stock y la cantidad pedida.
    assert "Yogur bebible" in data["message"]
    assert "10" in data["message"]
    assert f"stock actual es {STOCK_INICIAL}" in data["message"]

    stock, balance, mermas, auditorias = await _estado(session, escenario)
    assert stock == STOCK_INICIAL
    assert balance == STOCK_INICIAL
    assert mermas == []
    assert auditorias == 0


async def test_merma_igual_al_stock_lo_deja_en_cero(
    session: AsyncSession, escenario: Any
) -> None:
    data, action = await _confirmar_merma(session, escenario, qty=STOCK_INICIAL)

    assert data["execution_status"] == "SUCCEEDED"
    assert action.execution_status == "SUCCEEDED"
    stock, balance, mermas, auditorias = await _estado(session, escenario)
    assert stock == 0
    assert balance == 0
    assert mermas == [-STOCK_INICIAL]
    assert auditorias == 1


@pytest.mark.parametrize("qty", [1, 2])
async def test_merma_menor_que_el_stock_se_registra(
    session: AsyncSession, escenario: Any, qty: int
) -> None:
    data, action = await _confirmar_merma(session, escenario, qty=qty)

    assert data["execution_status"] == "SUCCEEDED"
    assert action.execution_status == "SUCCEEDED"
    stock, balance, mermas, auditorias = await _estado(session, escenario)
    assert stock == STOCK_INICIAL - qty
    assert balance == STOCK_INICIAL - qty
    assert mermas == [-qty]
    assert auditorias == 1
