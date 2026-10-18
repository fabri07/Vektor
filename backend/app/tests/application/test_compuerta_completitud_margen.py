"""La compuerta de completitud, igual en todo lector del margen (revisión de la fase 2).

Antes sólo la pantalla miraba la completitud, y sin ningún score mostraba los
márgenes igual; el chat, el punto de equilibrio y el agente de salud no la
miraban. Ahora todos pasan por ``completitud_insuficiente``: score < 50 o sin
score → sin margen, con el motivo. El BSL no, porque la completitud sale de él.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agents.chat.agent import AgentChat
from app.application.agents.expense.agent import AgentExpense
from app.application.agents.shared.schemas import ActionType, AgentRequest, AgentTask
from app.application.services import facts_provider
from app.domain.business_time import today_ar
from app.persistence.models.score import HealthScoreSnapshot
from app.persistence.models.transaction import SaleEntry
from app.tests.conftest import agregar_score_suficiente


async def _ventas(db: AsyncSession, tid: uuid.UUID) -> None:
    db.add_all([
        SaleEntry(
            tenant_id=tid, amount=Decimal("10000"), quantity=1, payment_method="cash",
            transaction_date=datetime.combine(today_ar() - timedelta(days=d), time(12)),
            cost_unit_ars=Decimal("6000"), cost_ars=Decimal("6000"), cost_confirmed=True,
            cost_origin="compra", cost_snapshot_version=1,
        )
        for d in range(3)
    ])
    await db.commit()


async def _score(db: AsyncSession, tid: uuid.UUID, completitud: str) -> None:
    ahora = datetime.now(UTC)
    db.add(HealthScoreSnapshot(
        tenant_id=tid, total_score=40, level="WARNING", dimensions=[], triggered_by="test",
        snapshot_date=ahora, created_at=ahora,
        data_completeness_score=Decimal(completitud), confidence_level="LOW",
    ))
    await db.commit()


@pytest.fixture(params=["sin_score", "completitud_40"])
async def sin_datos_suficientes(
    request: pytest.FixtureRequest, db_session: AsyncSession, sample_tenant: Any
) -> uuid.UUID:
    tid: uuid.UUID = sample_tenant.tenant_id
    await _ventas(db_session, tid)
    if request.param == "completitud_40":
        await agregar_score_suficiente(db_session, tid)  # el de ayer no manda
        await _score(db_session, tid, "40")
    return tid


async def test_leer_margenes_con_compuerta_no_da_margen(
    db_session: AsyncSession, sin_datos_suficientes: uuid.UUID
) -> None:
    lectura = await facts_provider.leer_margenes(
        db_session, sin_datos_suficientes, compuerta=True
    )
    assert (lectura.margenes, lectura.motivo) == (None, facts_provider.MOTIVO_POCOS_DATOS)
    # El BSL la lee sin compuerta: la completitud se calcula con esto.
    assert (await facts_provider.leer_margenes(db_session, sin_datos_suficientes)).margenes


async def test_la_pantalla_no_muestra_margenes(
    client: AsyncClient, auth_headers: dict[str, str], sin_datos_suficientes: uuid.UUID
) -> None:
    body = (await client.get("/api/v1/insights/margins", headers=auth_headers)).json()
    assert body["sin_datos"] is True


async def test_el_chat_no_dice_el_margen(
    db_session: AsyncSession, sin_datos_suficientes: uuid.UUID
) -> None:
    contexto = await AgentChat()._load_financial_context(sin_datos_suficientes, db_session)
    assert f"Margen neto: sin medir ({facts_provider.MOTIVO_POCOS_DATOS})" in contexto
    assert "Margen bruto del mes en curso" not in contexto


async def test_el_punto_de_equilibrio_no_se_calcula(
    db_session: AsyncSession, sin_datos_suficientes: uuid.UUID
) -> None:
    tid = sin_datos_suficientes
    resp = await AgentExpense(db=db_session).process(
        AgentRequest(user_id=str(uuid.uuid4()), business_id=str(tid), message=""),
        task=AgentTask(
            agent="agent_expense", action_type=ActionType.ANALYZE_EXPENSE_DATA,
            entities={"_intent": "calcular_punto_equilibrio"},
        ),
    )
    assert resp.result["summary"] == "equilibrio_sin_margen"
    assert facts_provider.MOTIVO_POCOS_DATOS in (resp.message or "")


async def test_con_score_suficiente_si_hay_margen(
    db_session: AsyncSession, sample_tenant: Any
) -> None:
    tid = sample_tenant.tenant_id
    await _ventas(db_session, tid)
    await agregar_score_suficiente(db_session, tid)
    lectura = await facts_provider.leer_margenes(db_session, tid, compuerta=True)
    assert lectura.margenes is not None
    assert lectura.margenes.bruto.value == 40.0
