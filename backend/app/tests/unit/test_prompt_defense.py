from app.application.security.prompt_defense import is_valid_action_type


def test_update_product_is_valid_action_type() -> None:
    assert is_valid_action_type("UPDATE_PRODUCT") is True


def test_answer_data_query_is_valid_action_type() -> None:
    """ANSWER_DATA_QUERY (Fase 5) está en VALID_ACTION_TYPES."""
    assert is_valid_action_type("ANSWER_DATA_QUERY") is True


def test_unknown_action_type_is_invalid() -> None:
    assert is_valid_action_type("HACK_THE_SYSTEM") is False
