"""Tests del handler RECLASSIFY_EXPENSE en AgentExpense (Nivel 2).

Cubre:
- Asesoría read-only (reventa vs insumo) según el vertical, sin DB.
- Construcción del pending action MEDIUM/requires_approval cuando hay
  identificación + target.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.application.agents.expense.agent import AgentExpense
from app.application.agents.shared.schemas import (
    ActionType,
    AgentRequest,
    AgentTask,
    RiskLevel,
)
from app.domain.verticals import Vertical

_TENANT = "00000000-0000-0000-0000-000000000001"
_USER = "00000000-0000-0000-0000-000000000002"


def _req(message: str) -> AgentRequest:
    return AgentRequest(user_id=_USER, business_id=_TENANT, message=message)


def _agent() -> AgentExpense:
    """Sin sesión de DB el agente no puede leer el vertical del tenant: el default
    lo inyecta el test, no el código de producción."""
    return AgentExpense(default_vertical=Vertical.KIOSCO_ALMACEN)


def _task(entities: dict[str, Any] | None = None) -> AgentTask:
    return AgentTask(
        agent="agent_expense",
        action_type=ActionType.RECLASSIFY_EXPENSE,
        entities=entities or {},
    )


# ── Asesoría (read-only, sin DB → vertical inyectado por el test) ─────────────


@pytest.mark.asyncio
async def test_advice_golosinas_is_reventa_in_kiosco():
    """Golosinas en kiosco → reventa (mercadería / INVENTORY)."""
    agent = _agent()
    res = await agent.process(
        _req("compré golosinas, ¿esto es mercadería o insumo?"),
        task=_task({"descripcion": "golosinas"}),
    )
    assert res.status == "success"
    assert res.risk_level == RiskLevel.LOW
    assert res.requires_approval is False
    assert res.result["structured_data"]["es_reventa"] is True
    assert res.result["structured_data"]["recommended_category"] == "INVENTORY"
    assert res.result["structured_data"]["recommended_target"] == "reventa"


@pytest.mark.asyncio
async def test_advice_bolsas_is_insumo():
    """Bolsas / artículos de uso interno → insumo (OPEX / SUPPLIES)."""
    agent = _agent()
    res = await agent.process(
        _req("compré bolsas para el local"),
        task=_task({"descripcion": "bolsas"}),
    )
    assert res.status == "success"
    assert res.result["structured_data"]["es_reventa"] is False
    assert res.result["structured_data"]["recommended_category"] == "SUPPLIES"
    assert res.result["structured_data"]["recommended_target"] == "insumo"


@pytest.mark.asyncio
async def test_advice_alquiler_is_other_category():
    """Alquiler → no es reventa ni insumo: categoría operativa."""
    agent = _agent()
    res = await agent.process(
        _req("el alquiler, ¿cómo lo clasifico?"),
        task=_task({"descripcion": "alquiler"}),
    )
    assert res.status == "success"
    assert res.result["structured_data"]["es_reventa"] is False
    assert res.result["structured_data"]["recommended_category"] == "RENT"
    assert res.result["structured_data"]["recommended_target"] == "categoria"


# ── Escritura: pending action MEDIUM ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_write_reventa_builds_medium_pending_action():
    agent = _agent()
    res = await agent.process(
        _req("marcá ese gasto como reventa"),
        task=_task(
            {
                "expense_id": "11111111-1111-1111-1111-111111111111",
                "target": "reventa",
                "sku": "GOL-001",
            }
        ),
    )
    assert res.status == "requires_approval"
    assert res.risk_level == RiskLevel.MEDIUM
    assert res.requires_approval is True
    sd = res.result["structured_data"]
    assert res.result["action_type"] == ActionType.RECLASSIFY_EXPENSE
    assert sd["target"] == "reventa"
    assert sd["category"] == "INVENTORY"
    assert sd["sku"] == "GOL-001"
    assert sd["expense_id"] == "11111111-1111-1111-1111-111111111111"


@pytest.mark.asyncio
async def test_write_insumo_builds_medium_pending_action():
    agent = _agent()
    res = await agent.process(
        _req("esto es insumo"),
        task=_task(
            {
                "descripcion": "rollos de papel",
                "monto": "5000",
                "target": "insumo",
            }
        ),
    )
    assert res.status == "requires_approval"
    assert res.risk_level == RiskLevel.MEDIUM
    sd = res.result["structured_data"]
    assert sd["target"] == "insumo"
    assert sd["category"] == "SUPPLIES"


@pytest.mark.asyncio
async def test_write_categoria_normalizes_code():
    agent = _agent()
    res = await agent.process(
        _req("movelo a impuestos"),
        task=_task(
            {
                "descripcion": "pago AFIP",
                "target": "categoria",
                "category": "impuestos",
            }
        ),
    )
    assert res.status == "requires_approval"
    sd = res.result["structured_data"]
    assert sd["target"] == "categoria"
    assert sd["category"] == "TAXES"


@pytest.mark.asyncio
async def test_no_identification_falls_to_advice():
    """Target sin identificación de gasto → asesoría, no escritura."""
    agent = _agent()
    res = await agent.process(
        _req("quiero reclasificar algo a reventa"),
        task=_task({"target": "reventa"}),
    )
    assert res.status == "success"
    assert res.requires_approval is False
