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

import contextlib
import uuid
from contextlib import ExitStack
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.application.agents.shared.alert_explainer import (
    _ALERT_MEANING,
    _RISK_CODE_TO_METRICS,
    explain_alerts,
    resolve_alert_facts,
)
from app.application.agents.shared.intent_rescue import normalize
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
    # El orchestrator aplica el regex sobre el mensaje NORMALIZADO (sin tildes).
    assert _ALERT_EXPLAIN_RE.search(normalize(message))


@pytest.mark.parametrize(
    "message",
    [
        "cuánto vendí ayer",
        "registrá una venta de 5000",
        "configurame una alerta para stock bajo",
        "explicame el margen bruto",
        "cargá el gasto del alquiler",
        # 'arroja' contiene 'roja' — sin word boundaries esto era un hijack real
        "que es lo que arroja el reporte de ventas",
    ],
)
def test_explain_regex_no_false_positives(message: str) -> None:
    assert not _ALERT_EXPLAIN_RE.search(normalize(message))


def test_meaning_catalog_parity_with_insight_templates() -> None:
    """Un risk_code nuevo en insight_templates SIN entrada acá debe romper CI,
    no degradar en producción a 'no encontré los datos'."""
    from app.heuristics.insight_templates import TEMPLATES

    assert set(TEMPLATES.keys()) == set(_RISK_CODE_TO_METRICS.keys())
    assert set(TEMPLATES.keys()) == set(_ALERT_MEANING.keys())


# ── resolve_alert_facts ───────────────────────────────────────────────────────


def _fact(fact_id: str, metric: str, value: float | None, severity: str | None) -> BusinessFact:
    return BusinessFact(
        fact_id=fact_id,
        domain="caja",
        metric=metric,
        value=value,
        unit="ARS",
        period="últimos_30_días",
        severity=severity,
        provenance=Provenance.REAL,
        confidence=1.0,
        sample_size=40,
        source="sales",
    )


def _mock_facts_service(
    snapshot: dict[str, BusinessFact],
    sobrestock: list[BusinessFact] | None = None,
) -> MagicMock:
    svc = MagicMock()
    svc.dashboard_snapshot.return_value = snapshot
    svc.sobrestock.return_value = sobrestock or []
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


def test_resolve_risk_code_without_fact_severity_still_alert() -> None:
    """EL bug del review: caja_liquida nunca setea severity → still_alert daba
    False y el LLM decía 'ya no está en alerta' con el banner rojo en pantalla.
    Para risk_codes manda el health engine: still_alert=True."""
    caja = _fact("caja_liquida_últimos_30_días", "caja_liquida", 15000.0, None)
    svc = _mock_facts_service({"caja_liquida": caja})
    blocks = resolve_alert_facts(svc, "t1", ["CASH_LOW"], Period.last_n_days(30))
    assert blocks[0]["still_alert"] is True


def test_resolve_passes_include_demo() -> None:
    """Tenants demo: el chat debe ver los MISMOS números que el dashboard."""
    caja = _fact("caja_liquida_últimos_30_días", "caja_liquida", 15000.0, None)
    svc = _mock_facts_service({"caja_liquida": caja})
    resolve_alert_facts(
        svc, "t1", ["CASH_LOW"], Period.last_n_days(30), include_demo=True
    )
    assert svc.dashboard_snapshot.call_args.kwargs["include_demo"] is True
    assert svc.sobrestock.call_args.kwargs["include_demo"] is True


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


def test_resolve_direct_fact_id_no_longer_red() -> None:
    """fact_id directo que dejó de estar en rojo → se informa el estado actual
    (still_alert=False), NUNCA se descarta con 'no encontré los datos'."""
    margen = _fact("margen_neto_últimos_30_días", "margen_neto", 25.0, None)
    svc = _mock_facts_service({"margen_neto": margen})
    blocks = resolve_alert_facts(
        svc, "t1", ["margen_neto_últimos_30_días"], Period.last_n_days(30)
    )
    assert len(blocks) == 1
    assert blocks[0]["still_alert"] is False
    assert blocks[0]["fact"]["value"] == 25.0


# ── explain_alerts (narrador) ────────────────────────────────────────────────


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
    await_args = client.messages.create.await_args
    assert await_args is not None
    system = await_args.kwargs["system"]
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
            "fact": {
                "fact_id": "caja_liquida_últimos_30_días",
                "value": 15000.0,
                "confidence": 1.0,
            },
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
    assert response.confidence == Confidence.HIGH  # min(confidence)=1.0 → HIGH


async def test_dispatchable_intent_is_never_hijacked(mock_db, mock_redis) -> None:
    """Invariante 1: un intent despachable gana SIEMPRE, aunque el mensaje
    matchee el regex y haya alertas activas en ui_context."""
    request = _make_request(
        "vendí 3 gaseosas a 2000, y explicame el cartel rojo",
        ui_context={"active_alert_ids": ["CASH_LOW"]},
    )
    with ExitStack() as stack:
        # CEO clasifica registrar_venta (despachable, no overridable)
        _enter_common_patches(stack, "registrar_venta")
        stack.enter_context(patch(f"{ORCHESTRATOR}.TeamPlanExecutor"))
        explain_mock = stack.enter_context(
            patch(
                "app.application.services.facts_provider.build_facts_service",
                AsyncMock(),
            )
        )
        # El plan mockeado puede fallar aguas abajo; lo que importa es que NO
        # se tomó el camino del explainer.
        with contextlib.suppress(Exception):
            await ChatOrchestrator().handle(
                request, mock_db, mock_redis, uuid.uuid4(), uuid.uuid4()
            )

    explain_mock.assert_not_awaited()


async def test_malformed_alert_ids_do_not_crash(mock_db, mock_redis) -> None:
    """active_alert_ids no-lista (int) o string → se ignora, sin TypeError/500."""
    bad_ids_cases: list[object] = [42, "CASH_LOW", {"x": 1}, [None, "", {}]]
    for bad_ids in bad_ids_cases:
        request = _make_request(
            "explicame el mensaje en rojo",
            ui_context={"active_alert_ids": bad_ids},
        )
        with ExitStack() as stack:
            _enter_common_patches(stack, "out_of_scope")
            gap_cls = stack.enter_context(patch(f"{GAP_SERVICE}.CoverageGapService"))
            gap_cls.return_value.log_gap = AsyncMock()
            response = await ChatOrchestrator().handle(
                request, mock_db, mock_redis, uuid.uuid4(), uuid.uuid4()
            )
        # Sin ids válidos → cae al pedido amable de ui_context, nunca 500.
        assert response.message == _UI_CONTEXT_MISSING_MESSAGE


async def test_aclaracion_archivo_is_not_derailed(mock_db, mock_redis) -> None:
    """pedir_aclaracion_sobre_archivo conserva su pregunta específica aunque el
    mensaje matchee el regex de explicación y no haya ui_context."""
    from app.application.services.chat_orchestrator import _NO_AGENT_MESSAGES

    request = _make_request(
        "explicame el mensaje de que el archivo necesita completar datos",
        ui_context=None,
    )
    with ExitStack() as stack:
        _enter_common_patches(stack, "pedir_aclaracion_sobre_archivo")
        gap_cls = stack.enter_context(patch(f"{GAP_SERVICE}.CoverageGapService"))
        gap_cls.return_value.log_gap = AsyncMock()
        response = await ChatOrchestrator().handle(
            request, mock_db, mock_redis, uuid.uuid4(), uuid.uuid4()
        )

    assert response.message == _NO_AGENT_MESSAGES["pedir_aclaracion_sobre_archivo"]


async def test_llm_failure_degrades_gracefully(mock_db, mock_redis) -> None:
    """Un error de Anthropic en explain_alerts NO escapa como 500: respuesta
    amable + confidence LOW, mismo degrade que el resto de los paths."""
    request = _make_request(
        "por qué está en rojo este mensaje",
        ui_context={"active_alert_ids": ["CASH_LOW"]},
    )
    fake_blocks = [
        {
            "alert_id": "CASH_LOW",
            "fact": {"fact_id": "x", "value": 1.0, "confidence": 1.0},
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
                AsyncMock(side_effect=RuntimeError("anthropic 529")),
            )
        )
        response = await ChatOrchestrator().handle(
            request, mock_db, mock_redis, uuid.uuid4(), uuid.uuid4()
        )

    assert response.status == "success"
    assert "no pude armar la explicación" in (response.message or "").lower()
    assert response.confidence == Confidence.LOW


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
    await_args = gap_cls.return_value.log_gap.await_args
    assert await_args is not None
    assert await_args.kwargs["fallback_reason"] == "ui_context_missing"


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
    await_args = gap_cls.return_value.log_gap.await_args
    assert await_args is not None
    assert await_args.kwargs["fallback_reason"] == "sin_datos"
