"""Tests de la explicación de alertas del dashboard (Parte B).

Cubre:
- Detección determinística (regex deíctico) sin falsos positivos obvios.
- resolve_alert_facts: risk_code → BusinessFact fresco; fact_id directo;
  id desconocido se salta; SUPPLIER_DEPENDENCY sin fact → solo significado.
- explain_alerts: prompt con registro llano + no-invention; LLMCall correcto.
- Orquestación: con ui_context+alerta → explica con el número real; sin
  ui_context → gap 'ui_context_missing' + pedido amable; regresión de mensajes
  normales (cubierta también en test_coverage_gaps).
"""

from __future__ import annotations

import uuid
from contextlib import ExitStack
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.application.agents.shared.alert_explainer import (
    explain_alerts,
    resolve_alert_facts,
)
from app.application.agents.shared.schemas import (
    AgentRequest,
    AgentResponse,
    Confidence,
    RiskLevel,
)
from app.application.services.chat_orchestrator import (
    _ALERT_EXPLAIN_RE,
    _UI_CONTEXT_MISSING_MESSAGE,
    ChatOrchestrator,
)
from app.application.services.facts_service import BusinessFact, Period, Provenance

ORCHESTRATOR = "app.application.services.chat_orchestrator"
EXPLAINER = "app.application.agents.shared.alert_explainer"
GAP_SERVICE = "app.application.services.coverage_gap_service"


# ── Regex de detección ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "message",
    [
        "explicame el mensaje en rojo",
        "por qué está en rojo eso",
        "porque esta en rojo la caja",  # sin tildes (rioplatense real)
        "qué significa esta alerta",
        "explicame ese cartel rojo",
        "que quiere decir el aviso rojo",
    ],
)
def test_explain_regex_matches(message: str) -> None:
    assert _ALERT_EXPLAIN_RE.search(message)


@pytest.mark.parametrize(
    "message",
    [
        "cuánto vendí ayer",
        "registrá una venta de 5000",
        "configurame una alerta para stock bajo",
        "explicame el margen bruto",
        "cargá el gasto del alquiler",
    ],
)
def test_explain_regex_no_false_positives(message: str) -> None:
    assert not _ALERT_EXPLAIN_RE.search(message)


# ── resolve_alert_facts ───────────────────────────────────────────────────────


def _fact(fact_id: str, metric: str, value: float | None, severity: str | None) -> BusinessFact:
    return BusinessFact(
        fact_id=fact_id,
        domain="caja",
        metric=metric,
        value=value,
        unit="ARS",
        period="últimos_30_días",
        severity=severity,  # type: ignore[arg-type]
        provenance=Provenance.REAL,
        confidence=1.0,
        sample_size=40,
        source="sales",
    )


def _mock_facts_service(snapshot: dict[str, BusinessFact]) -> MagicMock:
    svc = MagicMock()
    svc.dashboard_snapshot.return_value = snapshot
    # get_alert_by_id solo resuelve fact_ids exactos con severity activa
    def _get(tenant_id: str, alert_id: str, p: Period) -> BusinessFact | None:
        for f in snapshot.values():
            if f.fact_id == alert_id and f.severity in ("warning", "critical"):
                return f
        return None

    svc.get_alert_by_id.side_effect = _get
    return svc


def test_resolve_risk_code_maps_to_fact() -> None:
    caja = _fact("caja_liquida_últimos_30_días", "caja_liquida", 15000.0, "warning")
    svc = _mock_facts_service({"caja_liquida": caja})
    blocks = resolve_alert_facts(svc, "t1", ["CASH_LOW"], Period.last_n_days(30))
    assert len(blocks) == 1
    assert blocks[0]["fact"]["metric"] == "caja_liquida"
    assert blocks[0]["fact"]["value"] == 15000.0
    assert blocks[0]["still_alert"] is True
    assert "Caja baja" in blocks[0]["meaning"]


def test_resolve_direct_fact_id() -> None:
    margen = _fact("margen_neto_últimos_30_días", "margen_neto", 8.0, "critical")
    svc = _mock_facts_service({"margen_neto": margen})
    blocks = resolve_alert_facts(
        svc, "t1", ["margen_neto_últimos_30_días"], Period.last_n_days(30)
    )
    assert len(blocks) == 1
    assert blocks[0]["fact"]["fact_id"] == "margen_neto_últimos_30_días"


def test_resolve_unknown_id_is_skipped() -> None:
    svc = _mock_facts_service({})
    blocks = resolve_alert_facts(svc, "t1", ["ALERTA_INVENTADA"], Period.last_n_days(30))
    assert blocks == []


def test_resolve_supplier_dependency_without_fact() -> None:
    """SUPPLIER_DEPENDENCY no tiene BusinessFact → solo significado funcional."""
    svc = _mock_facts_service({})
    blocks = resolve_alert_facts(
        svc, "t1", ["SUPPLIER_DEPENDENCY"], Period.last_n_days(30)
    )
    assert len(blocks) == 1
    assert blocks[0]["fact"] is None
    assert "proveedor" in blocks[0]["meaning"].lower()


def test_resolve_alert_no_longer_red() -> None:
    """La alerta se apagó (datos cambiaron) → se informa el estado actual, no error."""
    caja = _fact("caja_liquida_últimos_30_días", "caja_liquida", 90000.0, "info")
    svc = _mock_facts_service({"caja_liquida": caja})
    blocks = resolve_alert_facts(svc, "t1", ["CASH_LOW"], Period.last_n_days(30))
    assert len(blocks) == 1
    assert blocks[0]["still_alert"] is False
    assert blocks[0]["fact"]["value"] == 90000.0


# ── explain_alerts (narrador) ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_explain_alerts_prompt_and_llm_call() -> None:
    caja = _fact("caja_liquida_últimos_30_días", "caja_liquida", 15000.0, "warning")
    blocks = [
        {
            "alert_id": "CASH_LOW",
            "fact": caja.model_dump(),
            "meaning": "Caja baja: la plata líquida viene floja.",
            "still_alert": True,
        }
    ]
    client = MagicMock()
    resp = MagicMock()
    resp.content = [MagicMock(text="Tu caja tiene $15.000 …")]
    resp.usage.input_tokens = 100
    resp.usage.output_tokens = 50
    client.messages.create = AsyncMock(return_value=resp)

    text, call = await explain_alerts(
        "por qué está en rojo", blocks, "Kiosco El Rápido", client
    )

    assert text == "Tu caja tiene $15.000 …"
    assert call.source == "alert_explainer"
    system = client.messages.create.await_args.kwargs["system"]
    # No-invention + registro llano + el número real presente en el prompt
    assert "NO inventes" in system
    assert "Plata antes que porcentajes" in system
    assert "15000" in system
    assert "caja_liquida_últimos_30_días" in system


# ── Orquestación end-to-end (mocks) ──────────────────────────────────────────


def _make_request(
    message: str,
    ui_context: dict[str, Any] | None = None,
) -> AgentRequest:
    return AgentRequest(
        user_id=str(uuid.uuid4()),
        business_id=str(uuid.uuid4()),
        message=message,
        attachments=[],
        conversation_id=None,
        ui_context=ui_context,
    )


def _ceo_response(request_id: str, intent: str) -> AgentResponse:
    return AgentResponse(
        request_id=request_id,
        agent_name="agent_ceo",
        status="success",
        risk_level=RiskLevel.LOW,
        requires_approval=False,
        confidence=Confidence.LOW,
        result={"intent": intent, "target_agent": None},
    )


@pytest.fixture
def mock_db():
    db = AsyncMock()
    tenant = MagicMock()
    tenant.display_name = "Kiosco El Rápido"
    db.get = AsyncMock(return_value=tenant)
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = MagicMock(vertical_code="kiosco_almacen")
    db.execute = AsyncMock(return_value=result_mock)
    return db


@pytest.fixture
def mock_redis():
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.setex = AsyncMock()
    return redis


def _enter_common_patches(stack: ExitStack, ceo_intent: str) -> None:
    stack.enter_context(
        patch(
            f"{ORCHESTRATOR}.AgentCEO",
            return_value=MagicMock(
                process=AsyncMock(
                    side_effect=lambda req: _ceo_response(req.request_id, ceo_intent)
                )
            ),
        )
    )
    stack.enter_context(patch(f"{ORCHESTRATOR}.get_anthropic_async_client"))
    stack.enter_context(patch(f"{ORCHESTRATOR}.AgentChat"))
    stack.enter_context(patch(f"{ORCHESTRATOR}.BusinessMemoryService"))
    stack.enter_context(patch(f"{ORCHESTRATOR}.AgentMemoryService"))


@pytest.mark.asyncio
async def test_explain_with_ui_context_returns_explanation(mock_db, mock_redis) -> None:
    """ui_context con alerta activa + pedido de explicación → explica con datos."""
    from app.application.agents.shared.schemas import LLMCall

    request = _make_request(
        "por qué está en rojo este mensaje",
        ui_context={"view": "dashboard", "active_alert_ids": ["CASH_LOW"]},
    )
    fake_blocks = [
        {
            "alert_id": "CASH_LOW",
            "fact": {"fact_id": "caja_liquida_últimos_30_días", "value": 15000.0},
            "meaning": "Caja baja.",
            "still_alert": True,
        }
    ]
    with ExitStack() as stack:
        _enter_common_patches(stack, "intent_desconocido")
        stack.enter_context(
            patch(
                "app.application.services.facts_provider.build_facts_service",
                AsyncMock(return_value=MagicMock()),
            )
        )
        stack.enter_context(
            patch(f"{EXPLAINER}.resolve_alert_facts", return_value=fake_blocks)
        )
        stack.enter_context(
            patch(
                f"{EXPLAINER}.explain_alerts",
                AsyncMock(
                    return_value=(
                        "Tu caja tiene $15.000, por eso el aviso.",
                        LLMCall(
                            source="alert_explainer",
                            model="claude-sonnet-4-6",
                            input_tokens=1,
                            output_tokens=1,
                        ),
                    )
                ),
            )
        )
        response = await ChatOrchestrator().handle(
            request, mock_db, mock_redis, uuid.uuid4(), uuid.uuid4()
        )

    assert response.message == "Tu caja tiene $15.000, por eso el aviso."
    assert response.result.get("alert_explanation") is True
    assert response.result.get("alert_ids") == ["CASH_LOW"]


@pytest.mark.asyncio
async def test_explain_without_ui_context_logs_gap(mock_db, mock_redis) -> None:
    """Pide explicar el rojo pero sin ui_context → gap + pedido amable, sin LLM."""
    request = _make_request("explicame el mensaje en rojo", ui_context=None)
    with ExitStack() as stack:
        _enter_common_patches(stack, "out_of_scope")
        gap_cls = stack.enter_context(patch(f"{GAP_SERVICE}.CoverageGapService"))
        gap_cls.return_value.log_gap = AsyncMock()
        response = await ChatOrchestrator().handle(
            request, mock_db, mock_redis, uuid.uuid4(), uuid.uuid4()
        )

    assert response.message == _UI_CONTEXT_MISSING_MESSAGE
    kwargs = gap_cls.return_value.log_gap.await_args.kwargs
    assert kwargs["fallback_reason"] == "ui_context_missing"


@pytest.mark.asyncio
async def test_unresolvable_alert_ids_get_honest_message(mock_db, mock_redis) -> None:
    """ui_context con id irresoluble → mensaje honesto + gap sin_datos."""
    request = _make_request(
        "qué significa esta alerta",
        ui_context={"active_alert_ids": ["ALERTA_INVENTADA"]},
    )
    with ExitStack() as stack:
        _enter_common_patches(stack, "intent_desconocido")
        stack.enter_context(
            patch(
                "app.application.services.facts_provider.build_facts_service",
                AsyncMock(return_value=MagicMock()),
            )
        )
        stack.enter_context(
            patch(f"{EXPLAINER}.resolve_alert_facts", return_value=[])
        )
        gap_cls = stack.enter_context(patch(f"{GAP_SERVICE}.CoverageGapService"))
        gap_cls.return_value.log_gap = AsyncMock()
        response = await ChatOrchestrator().handle(
            request, mock_db, mock_redis, uuid.uuid4(), uuid.uuid4()
        )

    assert "no encontré los datos" in (response.message or "").lower()
    kwargs = gap_cls.return_value.log_gap.await_args.kwargs
    assert kwargs["fallback_reason"] == "sin_datos"
