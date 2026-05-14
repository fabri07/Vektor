from app.application.security.prompt_defense import is_valid_action_type


def test_update_product_is_valid_action_type() -> None:
    assert is_valid_action_type("UPDATE_PRODUCT") is True
