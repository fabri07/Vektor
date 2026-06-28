from app.application.agents.shared.schemas import ActionType, RiskLevel

ACTION_RISK_MAP = {
    ActionType.REGISTER_SALE: RiskLevel.MEDIUM,
    ActionType.REGISTER_CASH_INFLOW: RiskLevel.MEDIUM,
    ActionType.REGISTER_EXPENSE: RiskLevel.MEDIUM,
    ActionType.REGISTER_PURCHASE: RiskLevel.MEDIUM,
    ActionType.REGISTER_CASH_OUTFLOW: RiskLevel.MEDIUM,
    ActionType.UPDATE_STOCK: RiskLevel.MEDIUM,
    ActionType.UPDATE_PRODUCT: RiskLevel.MEDIUM,
    ActionType.REGISTER_STOCK_LOSS: RiskLevel.HIGH,
    ActionType.IMPORT_TABULAR_FILE: RiskLevel.MEDIUM,
    ActionType.CREATE_PURCHASE_SUGGESTION: RiskLevel.LOW,
    ActionType.PARSE_DOCUMENT_FILE: RiskLevel.LOW,
    ActionType.GENERATE_HEALTH_REPORT: RiskLevel.LOW,
    ActionType.ANSWER_HELP_REQUEST: RiskLevel.LOW,
    ActionType.CREATE_SUPPLIER_DRAFT: RiskLevel.LOW,
    ActionType.CLASSIFY_GMAIL_MESSAGE: RiskLevel.LOW,
    ActionType.SYNC_TO_GOOGLE: RiskLevel.MEDIUM,
    ActionType.CREATE_CALENDAR_EVENT: RiskLevel.MEDIUM,
    # Stage 4: Google writes via broker
    ActionType.UPLOAD_TO_DRIVE: RiskLevel.MEDIUM,
    ActionType.CREATE_GOOGLE_DOC: RiskLevel.MEDIUM,
    ActionType.APPEND_TO_SHEET: RiskLevel.MEDIUM,
    # Sprint 17: acciones analíticas read-only — LOW, sin aprobación
    ActionType.ANALYZE_FILE: RiskLevel.LOW,
    ActionType.ANALYZE_PRICES: RiskLevel.LOW,
    ActionType.ANALYZE_STOCK_DATA: RiskLevel.LOW,
    ActionType.ANALYZE_SALES_DATA: RiskLevel.LOW,
    ActionType.ANALYZE_EXPENSE_DATA: RiskLevel.LOW,
    ActionType.ANALYZE_SUPPLIER_DATA: RiskLevel.LOW,
    ActionType.SIMULATE_SCENARIO: RiskLevel.LOW,
    ActionType.ANALYZE_MARKETING_DATA: RiskLevel.LOW,
    # Nivel 2: reclasificar un gasto muta su clasificación contable → requiere aprobación
    ActionType.RECLASSIFY_EXPENSE: RiskLevel.MEDIUM,
    # Fase 5: consulta libre read-only — LOW, sin aprobación
    ActionType.ANSWER_DATA_QUERY: RiskLevel.LOW,
    # v4 F6a: recordatorio WhatsApp click-to-chat — LOCAL, MEDIUM (requiere aprobación)
    ActionType.PREPARE_WHATSAPP_MESSAGE: RiskLevel.MEDIUM,
}


class RiskEngine:
    @staticmethod
    def evaluate(action_type: ActionType) -> RiskLevel:
        return ACTION_RISK_MAP.get(action_type, RiskLevel.HIGH)

    @staticmethod
    def requires_approval(action_type: ActionType) -> bool:
        return RiskEngine.evaluate(action_type) in (RiskLevel.MEDIUM, RiskLevel.HIGH)
