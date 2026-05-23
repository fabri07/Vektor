"""Tests para team_plan_builder — contrato del catálogo de intents y construcción de planes."""

import pytest

from app.application.agents.ceo.team_plan_builder import (
    INTENT_CATALOG,
    INTENT_TO_ACTION_TYPE,
    INTENT_TO_AGENT,
    build_plan,
)
from app.application.agents.shared.schemas import ActionType, AgentTeamPlan


# ── Catálogo ──────────────────────────────────────────────────────────────────

def test_intent_catalog_size():
    assert len(INTENT_CATALOG) == 17


def test_all_intents_have_agent_mapping():
    """Todo intent del catálogo tiene un agente destino."""
    for intent in INTENT_CATALOG:
        assert intent in INTENT_TO_AGENT, f"Intent sin agente: {intent}"


def test_all_intents_have_action_type_mapping():
    """Todo intent del catálogo tiene un ActionType."""
    for intent in INTENT_CATALOG:
        assert intent in INTENT_TO_ACTION_TYPE, f"Intent sin ActionType: {intent}"


def test_no_extra_mappings_vs_catalog():
    """No hay mappings huérfanos que no estén en INTENT_CATALOG."""
    catalog_set = set(INTENT_CATALOG)
    assert set(INTENT_TO_AGENT.keys()) == catalog_set
    assert set(INTENT_TO_ACTION_TYPE.keys()) == catalog_set


# ── build_plan — correctness ──────────────────────────────────────────────────

def test_build_plan_ingresar_venta():
    plan = build_plan("ingresar_venta", {"monto": 1500})
    assert isinstance(plan, AgentTeamPlan)
    assert len(plan.tasks) == 1
    assert plan.tasks[0].action_type == ActionType.REGISTER_SALE
    assert plan.tasks[0].agent == "agent_income"
    assert plan.tasks[0].entities["monto"] == 1500
    assert plan.intent == "ingresar_venta"
    assert plan.requires_synthesis is False


def test_build_plan_ingresar_gasto():
    plan = build_plan("ingresar_gasto", {"descripcion": "alquiler"})
    assert plan.tasks[0].action_type == ActionType.REGISTER_EXPENSE
    assert plan.tasks[0].agent == "agent_expense"


def test_build_plan_registrar_merma():
    plan = build_plan("registrar_merma", {})
    assert plan.tasks[0].action_type == ActionType.REGISTER_STOCK_LOSS
    assert plan.tasks[0].agent == "agent_stock"


def test_build_plan_consultar_estado():
    plan = build_plan("consultar_estado_negocio", {})
    assert plan.tasks[0].action_type == ActionType.GENERATE_HEALTH_REPORT
    assert plan.tasks[0].agent == "agent_health"


def test_build_plan_ayuda_plataforma():
    plan = build_plan("ayuda_plataforma", {})
    assert plan.tasks[0].action_type == ActionType.ANSWER_HELP_REQUEST
    assert plan.tasks[0].agent == "agent_helper"


def test_build_plan_intent_desconocido_fallback():
    plan = build_plan("intent_desconocido", {})
    assert plan.tasks[0].action_type == ActionType.ANSWER_HELP_REQUEST
    assert plan.tasks[0].agent == "agent_helper"


def test_build_plan_unknown_intent_fallback():
    """Intent no registrado cae en agent_helper / ANSWER_HELP_REQUEST."""
    plan = build_plan("esto_no_existe", {})
    assert plan.tasks[0].action_type == ActionType.ANSWER_HELP_REQUEST
    assert plan.tasks[0].agent == "agent_helper"


# ── build_plan — invariantes estructurales ────────────────────────────────────

def test_build_plan_always_single_task_stage1():
    """Stage 1: build_plan siempre retorna exactamente 1 tarea."""
    for intent in INTENT_CATALOG:
        plan = build_plan(intent, {})
        assert len(plan.tasks) == 1, f"build_plan({intent!r}) retornó {len(plan.tasks)} tasks"


def test_build_plan_generates_unique_ids():
    """Cada llamada a build_plan genera plan_id y task_id distintos."""
    plan1 = build_plan("ingresar_venta", {})
    plan2 = build_plan("ingresar_venta", {})
    assert plan1.plan_id != plan2.plan_id
    assert plan1.tasks[0].task_id != plan2.tasks[0].task_id


def test_build_plan_entities_isolated():
    """Las entities no son un mutable compartido entre instancias."""
    entities = {"monto": 100}
    plan = build_plan("ingresar_venta", entities)
    plan.tasks[0].entities["extra"] = "x"
    # El dict original no debe mutarse
    assert "extra" not in entities
