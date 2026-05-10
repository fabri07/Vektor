# Re-export all models so Alembic autogenerate can discover them.
from app.persistence.models.activity import UserActivityEvent
from app.persistence.models.agent_automation_rule import AgentAutomationRule
from app.persistence.models.analytics_event import AnalyticsEvent
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.auth_token import EmailVerificationToken, PasswordResetToken
from app.persistence.models.business import (
    ActionSuggestion,
    BusinessProfile,
    BusinessSnapshot,
    Insight,
    MomentumProfile,
)
from app.persistence.models.chat_session_log import ChatSessionLog
from app.persistence.models.conversation_context import AgentConversationContext
from app.persistence.models.file import UploadedFile
from app.persistence.models.google_mcp_connection import GoogleMcpConnection
from app.persistence.models.heuristic_override import BusinessHeuristicOverride
from app.persistence.models.inventory import InventoryBalance, InventoryMovement
from app.persistence.models.memory import AgentMemory, BusinessMemory, OperationFingerprint
from app.persistence.models.notification import Notification
from app.persistence.models.pending_action import PendingAction
from app.persistence.models.product import Product
from app.persistence.models.score import (
    HealthScoreSnapshot,
    HeuristicRuleSet,
    WeeklyScoreHistory,
)
from app.persistence.models.field_definitions import (
    TenantCustomFieldDefinition,
    TenantFieldChangeLog,
    VerticalFieldDefinition,
)
from app.persistence.models.tenant import Subscription, Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.persistence.models.repair import DataRepairItem, DataRepairRun
from app.persistence.models.user import User
from app.persistence.models.user_auth_identity import UserAuthIdentity

__all__ = [
    "UserActivityEvent",
    "AnalyticsEvent",
    "AgentAutomationRule",
    "Tenant",
    "Subscription",
    "User",
    "UserAuthIdentity",
    "BusinessProfile",
    "BusinessSnapshot",
    "HeuristicRuleSet",
    "HealthScoreSnapshot",
    "WeeklyScoreHistory",
    "MomentumProfile",
    "InventoryBalance",
    "InventoryMovement",
    "Product",
    "SaleEntry",
    "ExpenseEntry",
    "Insight",
    "ActionSuggestion",
    "DecisionAuditLog",
    "UploadedFile",
    "Notification",
    "EmailVerificationToken",
    "PasswordResetToken",
    "BusinessHeuristicOverride",
    "AgentConversationContext",
    "PendingAction",
    "OperationFingerprint",
    "BusinessMemory",
    "AgentMemory",
    "GoogleMcpConnection",
    "ChatSessionLog",
    "VerticalFieldDefinition",
    "TenantCustomFieldDefinition",
    "TenantFieldChangeLog",
    "DataRepairRun",
    "DataRepairItem",
]
