import uuid
from enum import StrEnum
from typing import Any, Literal

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
    # Stage 4: Google writes via GoogleToolBroker (ejecutados por PendingActionService)
    UPLOAD_TO_DRIVE = "UPLOAD_TO_DRIVE"
    CREATE_GOOGLE_DOC = "CREATE_GOOGLE_DOC"
    APPEND_TO_SHEET = "APPEND_TO_SHEET"
    # Sprint 17: acciones analíticas read-only (sin aprobación — no mutan datos)
    ANALYZE_FILE = "ANALYZE_FILE"  # type detection, resumen ejecutivo, normalización
    ANALYZE_PRICES = "ANALYZE_PRICES"  # márgenes, sugerencias, simulaciones, comparación listas
    ANALYZE_STOCK_DATA = "ANALYZE_STOCK_DATA"  # quiebres, sobrestock, reposición, días de stock
    ANALYZE_SALES_DATA = "ANALYZE_SALES_DATA"  # rentabilidad, productos estrella, ticket, clientes
    ANALYZE_EXPENSE_DATA = (
        "ANALYZE_EXPENSE_DATA"  # costos fijos/variables, anómalos, punto equilibrio
    )
    ANALYZE_SUPPLIER_DATA = "ANALYZE_SUPPLIER_DATA"  # ranking, dependencia, pedidos sugeridos
    SIMULATE_SCENARIO = "SIMULATE_SCENARIO"  # escenarios financieros what-if
    # Nivel 2: reclasificación contable de un gasto (muta clasificación → MEDIUM)
    RECLASSIFY_EXPENSE = "RECLASSIFY_EXPENSE"  # reventa (COGS) | insumo (OPEX) | otra categoría


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
    attachments: list[Any] = Field(default_factory=list)
    conversation_id: str | None = None
    # context: outputs upstream del DAG multi-task. Llave reservada: "upstream_outputs"
    # → dict[task_id, result_dict]. Vacío en single-task y en el primer nivel.
    context: dict[str, Any] = Field(default_factory=dict)
    # NOTA: NO hay agent_target — AgentCEO lo asigna internamente


class LLMCall(BaseModel):
    source: str  # "ceo" | "agent_cash" | "agent_health" | "orchestrator" | etc.
    model: str  # "claude-sonnet-4-5" | "claude-haiku-4-5-20251001" | etc.
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
    result: dict[str, Any] = Field(default_factory=dict)
    pending_action_id: str | None = None
    pending_action_ids: list[str] | None = None  # Stage 3: multi-task approval groups
    approval_group_id: str | None = None  # Stage 3: vincula PendingActions de un plan
    question: str | None = None  # usado cuando status=requires_clarification
    message: str | None = None  # respuesta conversacional rica generada por ChatOrchestrator
    usage: UsageSummary | None = None  # tokens consumidos en este turno


# ── Contratos de AgentTeamPlan (Stage 1) ──────────────────────────────────────


class AgentTask(BaseModel):
    task_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    agent: str  # "agent_income", "agent_stock", etc.
    action_type: ActionType
    entities: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)  # task_ids previos (DAG, Stage 3)
    approval_group: str | None = None  # tasks con mismo group → aprueban juntas


class AgentTeamPlan(BaseModel):
    plan_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    intent: str
    tasks: list[AgentTask] = Field(default_factory=list)
    requires_synthesis: bool = False  # True cuando CEO debe sintetizar multi-task
    fallback_message: str | None = None
