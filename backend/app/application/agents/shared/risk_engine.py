from app.application.agents.shared.schemas import ActionType, RiskLevel

ACTION_RISK_MAP = {
    ActionType.REGISTER_SALE: RiskLevel.MEDIUM,
    ActionType.REGISTER_CASH_INFLOW: RiskLevel.MEDIUM,
    ActionType.REGISTER_EXPENSE: RiskLevel.MEDIUM,
    ActionType.REGISTER_PURCHASE: RiskLevel.MEDIUM,
    ActionType.REGISTER_CASH_OUTFLOW: RiskLevel.MEDIUM,
    ActionType.UPDATE_STOCK: RiskLevel.MEDIUM,
    ActionType.REGISTER_STOCK_LOSS: RiskLevel.HIGH,
    ActionType.CREATE_SUPPLIER_DRAFT: RiskLevel.MEDIUM,
    ActionType.IMPORT_TABULAR_FILE: RiskLevel.MEDIUM,
    ActionType.SYNC_TO_GOOGLE: RiskLevel.MEDIUM,
    ActionType.CREATE_PURCHASE_SUGGESTION: RiskLevel.LOW,
    ActionType.PARSE_DOCUMENT_FILE: RiskLevel.LOW,
    ActionType.GENERATE_HEALTH_REPORT: RiskLevel.LOW,
    ActionType.CLASSIFY_GMAIL_MESSAGE: RiskLevel.LOW,
    ActionType.ANSWER_HELP_REQUEST: RiskLevel.LOW,
}


class RiskEngine:
    @staticmethod
    def evaluate(action_type: ActionType) -> RiskLevel:
        return ACTION_RISK_MAP.get(action_type, RiskLevel.HIGH)

    @staticmethod
    def requires_approval(action_type: ActionType) -> bool:
        return RiskEngine.evaluate(action_type) in (RiskLevel.MEDIUM, RiskLevel.HIGH)
