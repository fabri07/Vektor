"""Tests para WhatsAppChannel.build_click_to_chat_link (v4 F6a).

Verifica:
- Formato wa.me/<digitos>?text=<encoded>
- Normalización de teléfono (antepone 54 si falta)
- URL-encoding correcto de espacios y acentos
- Catálogo: PREPARE_WHATSAPP_MESSAGE → MEDIUM, is_valid_action_type True
- Routing: recordar_por_whatsapp → agent_client / PREPARE_WHATSAPP_MESSAGE
"""

from urllib.parse import unquote

import pytest


# ── Link builder ──────────────────────────────────────────────────────────────


def _build(to: str, body: str) -> str:
    from app.integrations.communication.whatsapp_channel import WhatsAppChannel

    return WhatsAppChannel.build_click_to_chat_link(to, body)


def test_formato_base():
    url = _build("5491134567890", "Hola mundo")
    assert url.startswith("https://wa.me/5491134567890?text=")


def test_antepone_54_cuando_falta():
    """Número sin 54 → se antepone el código de país Argentina."""
    url = _build("1134567890", "mensaje")
    assert url.startswith("https://wa.me/541134567890?text=")


def test_no_duplica_54():
    """Número que ya empieza con 54 no lo duplica."""
    url = _build("5491112345678", "ok")
    assert "wa.me/5491112345678?" in url
    assert "wa.me/545491" not in url


def test_quita_guiones_y_espacios():
    """Guiones, espacios y paréntesis son eliminados antes de normalizar."""
    url = _build("(011) 15-3456-7890", "texto")
    # Quita no-dígitos → "0111534567890" (13 dígitos) → sin 54 → "540111534567890"
    assert "wa.me/540111534567890?" in url


def test_encoding_espacios():
    """Los espacios en el body deben estar URL-encodeados."""
    url = _build("5491134567890", "Hola mundo")
    # quote(' ') = '%20'
    assert "%20" in url
    # Decodificar y verificar el contenido
    text_part = url.split("?text=")[1]
    assert unquote(text_part) == "Hola mundo"


def test_encoding_acentos():
    """Acentos y caracteres especiales AR deben estar URL-encodeados."""
    body = "Hola, te recuerdo que tenés un saldo pendiente de $1.500 con nosotros. ¡Gracias!"
    url = _build("5491134567890", body)
    text_part = url.split("?text=")[1]
    assert unquote(text_part) == body


def test_encoding_cuerpo_completo():
    """Cuerpo con nombre, monto y exclamación se redondea correctamente."""
    body = "Hola Juan, te recuerdo que tenés un saldo pendiente de $2.000 con nosotros. ¡Gracias!"
    url = _build("5491100001111", body)
    assert url.startswith("https://wa.me/5491100001111?text=")
    assert unquote(url.split("?text=")[1]) == body


# ── Catálogo: ActionType, RiskEngine, prompt_defense ─────────────────────────


def test_prepare_whatsapp_message_in_action_type():
    from app.application.agents.shared.schemas import ActionType

    assert ActionType.PREPARE_WHATSAPP_MESSAGE == "PREPARE_WHATSAPP_MESSAGE"


def test_prepare_whatsapp_message_risk_medium():
    from app.application.agents.shared.risk_engine import ACTION_RISK_MAP, RiskEngine
    from app.application.agents.shared.schemas import ActionType, RiskLevel

    assert ACTION_RISK_MAP[ActionType.PREPARE_WHATSAPP_MESSAGE] == RiskLevel.MEDIUM
    assert RiskEngine.evaluate(ActionType.PREPARE_WHATSAPP_MESSAGE) == RiskLevel.MEDIUM
    assert RiskEngine.requires_approval(ActionType.PREPARE_WHATSAPP_MESSAGE) is True


def test_prepare_whatsapp_message_is_valid_action_type():
    from app.application.security.prompt_defense import is_valid_action_type

    assert is_valid_action_type("PREPARE_WHATSAPP_MESSAGE") is True


def test_routing_recordar_por_whatsapp_to_agent_client():
    from app.application.agents.ceo.team_plan_builder import INTENT_TO_AGENT

    assert INTENT_TO_AGENT["recordar_por_whatsapp"] == "agent_client"


def test_routing_recordar_por_whatsapp_action_type():
    from app.application.agents.ceo.team_plan_builder import INTENT_TO_ACTION_TYPE
    from app.application.agents.shared.schemas import ActionType

    assert INTENT_TO_ACTION_TYPE["recordar_por_whatsapp"] == ActionType.PREPARE_WHATSAPP_MESSAGE
