"""prompt_defense — protección contra prompt injection y validación de action_types."""

VALID_ACTION_TYPES: frozenset[str] = frozenset(
    {
        "REGISTER_SALE",
        "REGISTER_CASH_INFLOW",
        "REGISTER_EXPENSE",
        "REGISTER_PURCHASE",
        "REGISTER_CASH_OUTFLOW",
        "UPDATE_STOCK",
        "UPDATE_PRODUCT",
        "REGISTER_STOCK_LOSS",
        "CREATE_PURCHASE_SUGGESTION",
        "IMPORT_TABULAR_FILE",
        "PARSE_DOCUMENT_FILE",
        "GENERATE_HEALTH_REPORT",
        "ANSWER_HELP_REQUEST",
        "CREATE_SUPPLIER_DRAFT",
        "CLASSIFY_GMAIL_MESSAGE",
        "SYNC_TO_GOOGLE",
        "CREATE_CALENDAR_EVENT",
        # Stage 4: Google writes via broker
        "UPLOAD_TO_DRIVE",
        "CREATE_GOOGLE_DOC",
        "APPEND_TO_SHEET",
        # Sprint 17: acciones analíticas read-only
        "ANALYZE_FILE",
        "ANALYZE_PRICES",
        "ANALYZE_STOCK_DATA",
        "ANALYZE_SALES_DATA",
        "ANALYZE_EXPENSE_DATA",
        "ANALYZE_SUPPLIER_DATA",
        "SIMULATE_SCENARIO",
        # Nivel 2: reclasificación contable
        "RECLASSIFY_EXPENSE",
        # v4: marketing analytics read-only
        "ANALYZE_MARKETING_DATA",
        # Fase 5: consulta libre de datos del negocio (read-only, LOW)
        "ANSWER_DATA_QUERY",
    }
)


def wrap_user_input(message: str) -> str:
    """
    Envuelve el input del usuario para prevenir prompt injection.
    Usar en TODOS los system prompts que procesen texto libre del usuario.
    """
    return f"<user_message>{message}</user_message>"


def is_valid_action_type(action_type: str) -> bool:
    """Valida que el output del LLM sea un action_type del catálogo cerrado."""
    return action_type in VALID_ACTION_TYPES
