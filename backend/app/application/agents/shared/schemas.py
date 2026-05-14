import uuid
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

AgentStatus = Literal[
    "success",
    "requires_approval",
    "requires_clarification",
    "requires_google_auth",
    "error",
]


class ActionType(StrEnum):
    REGISTER_SALE = "REGISTER_SALE"
    REGISTER_CASH_INFLOW = "REGISTER_CASH_INFLOW"
    REGISTER_EXPENSE = "REGISTER_EXPENSE"
    REGISTER_PURCHASE = "REGISTER_PURCHASE"
    REGISTER_CASH_OUTFLOW = "REGISTER_CASH_OUTFLOW"
    UPDATE_STOCK = "UPDATE_STOCK"
    UPDATE_PRODUCT = "UPDATE_PRODUCT"
    REGISTER_STOCK_LOSS = "REGISTER_STOCK_LOSS"
    CREATE_PURCHASE_SUGGESTION = "CREATE_PURCHASE_SUGGESTION"
    IMPORT_TABULAR_FILE = "IMPORT_TABULAR_FILE"
    PARSE_DOCUMENT_FILE = "PARSE_DOCUMENT_FILE"
    GENERATE_HEALTH_REPORT = "GENERATE_HEALTH_REPORT"
    ANSWER_HELP_REQUEST = "ANSWER_HELP_REQUEST"
    # Google MCP — operaciones externas via MCP server
    CREATE_SUPPLIER_DRAFT = "CREATE_SUPPLIER_DRAFT"
    CLASSIFY_GMAIL_MESSAGE = "CLASSIFY_GMAIL_MESSAGE"
    SYNC_TO_GOOGLE = "SYNC_TO_GOOGLE"
    CREATE_CALENDAR_EVENT = "CREATE_CALENDAR_EVENT"


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class Confidence(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class AgentRequest(BaseModel):
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str
    business_id: str
    message: str
    attachments: list = Field(default_factory=list)
    conversation_id: str | None = None
    # NOTA: NO hay agent_target — AgentCEO lo asigna internamente


class LLMCall(BaseModel):
    source: str        # "ceo" | "agent_cash" | "agent_health" | "orchestrator" | etc.
    model: str         # "claude-sonnet-4-5" | "claude-haiku-4-5-20251001" | etc.
    input_tokens: int
    output_tokens: int


class UsageSummary(BaseModel):
    calls: list[LLMCall] = Field(default_factory=list)

    @property
    def total_input(self) -> int:
        return sum(c.input_tokens for c in self.calls)

    @property
    def total_output(self) -> int:
        return sum(c.output_tokens for c in self.calls)

    @property
    def total(self) -> int:
        return self.total_input + self.total_output


class AgentResponse(BaseModel):
    request_id: str
    agent_name: str
    status: AgentStatus
    risk_level: RiskLevel
    requires_approval: bool = False
    confidence: Confidence = Confidence.HIGH
    result: dict = Field(default_factory=dict)
    pending_action_id: str | None = None
    question: str | None = None  # usado cuando status=requires_clarification
    message: str | None = None   # respuesta conversacional rica generada por ChatOrchestrator
    usage: UsageSummary | None = None  # tokens consumidos en este turno
