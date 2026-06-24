"""Tests del parser JSON robusto compartido por los agentes."""

from app.application.agents.shared.json_parse import parse_llm_json


def test_plain_json() -> None:
    assert parse_llm_json('{"intent": "registrar_venta"}') == {"intent": "registrar_venta"}


def test_strips_json_fences() -> None:
    raw = '```json\n{"answer": "Entrá a Cargar datos", "confidence": "HIGH"}\n```'
    parsed = parse_llm_json(raw)
    assert parsed is not None
    assert parsed["confidence"] == "HIGH"


def test_strips_bare_fences() -> None:
    assert parse_llm_json('```\n{"ok": true}\n```') == {"ok": True}


def test_extracts_object_with_surrounding_text() -> None:
    raw = 'Claro, acá tenés:\n{"answer": "x", "confidence": "MEDIUM"}\n¡Listo!'
    parsed = parse_llm_json(raw)
    assert parsed is not None
    assert parsed["answer"] == "x"


def test_object_with_nested_braces_and_strings() -> None:
    raw = '{"a": {"b": 1}, "msg": "tiene } adentro"}'
    parsed = parse_llm_json(raw)
    assert parsed is not None
    assert parsed["a"] == {"b": 1}
    assert parsed["msg"] == "tiene } adentro"


def test_returns_none_on_garbage() -> None:
    assert parse_llm_json("no hay json acá") is None


def test_returns_none_on_empty() -> None:
    assert parse_llm_json("") is None
    assert parse_llm_json(None) is None


def test_rejects_non_object_json() -> None:
    # Una lista o un escalar no son un objeto de respuesta válido.
    assert parse_llm_json("[1, 2, 3]") is None
    assert parse_llm_json('"solo texto"') is None
