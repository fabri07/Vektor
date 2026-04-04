"""prompt_defense — protección contra prompt injection y validación de action_types."""

VALID_ACTION_TYPES: frozenset[str] = frozenset({
    "REGISTER_SALE",
    "REGISTER_CASH_INFLOW",
    "REGISTER_EXPENSE",
    "REGISTER_PURCHASE",
    "REGISTER_CASH_OUTFLOW",
    "UPDATE_STOCK",
    "REGISTER_STOCK_LOSS",
    "CREATE_SUPPLIER_DRAFT",
    "CREATE_PURCHASE_SUGGESTION",
    "IMPORT_TABULAR_FILE",
    "PARSE_DOCUMENT_FILE",
    "GENERATE_HEALTH_REPORT",
    "SYNC_TO_GOOGLE",
    "CLASSIFY_GMAIL_MESSAGE",
    "ANSWER_HELP_REQUEST",
})


def wrap_user_input(message: str) -> str:
    """
    Envuelve el input del usuario para prevenir prompt injection.
    Usar en TODOS los system prompts que procesen texto libre del usuario.
    """
    return f"<user_message>{message}</user_message>"


def is_valid_action_type(action_type: str) -> bool:
    """Valida que el output del LLM sea un action_type del catálogo cerrado."""
    return action_type in VALID_ACTION_TYPES
