from enum import StrEnum
from pydantic import BaseModel
from typing import Optional
import uuid


class ActionType(StrEnum):
    REGISTER_SALE = "REGISTER_SALE"
    REGISTER_CASH_INFLOW = "REGISTER_CASH_INFLOW"
    REGISTER_EXPENSE = "REGISTER_EXPENSE"
    REGISTER_PURCHASE = "REGISTER_PURCHASE"
    REGISTER_CASH_OUTFLOW = "REGISTER_CASH_OUTFLOW"
    UPDATE_STOCK = "UPDATE_STOCK"
    REGISTER_STOCK_LOSS = "REGISTER_STOCK_LOSS"
    CREATE_SUPPLIER_DRAFT = "CREATE_SUPPLIER_DRAFT"
    CREATE_PURCHASE_SUGGESTION = "CREATE_PURCHASE_SUGGESTION"
    IMPORT_TABULAR_FILE = "IMPORT_TABULAR_FILE"
    PARSE_DOCUMENT_FILE = "PARSE_DOCUMENT_FILE"
    GENERATE_HEALTH_REPORT = "GENERATE_HEALTH_REPORT"
    SYNC_TO_GOOGLE = "SYNC_TO_GOOGLE"
    CLASSIFY_GMAIL_MESSAGE = "CLASSIFY_GMAIL_MESSAGE"
    ANSWER_HELP_REQUEST = "ANSWER_HELP_REQUEST"


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class Confidence(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class AgentRequest(BaseModel):
    request_id: str = str(uuid.uuid4())
    user_id: str
    business_id: str
    message: str
    attachments: list = []
    conversation_id: Optional[str] = None
    # NOTA: NO hay agent_target — AgentCEO lo asigna internamente


class AgentResponse(BaseModel):
    request_id: str
    agent_name: str
    status: str  # success | requires_approval | requires_clarification | error
    risk_level: RiskLevel
    requires_approval: bool = False
    confidence: Confidence = Confidence.HIGH
    result: dict = {}
    pending_action_id: Optional[str] = None
    question: Optional[str] = None  # usado cuando status=requires_clarification
    message: Optional[str] = None   # respuesta conversacional rica generada por ChatOrchestrator
