"""
Transaction domain entities: SaleEntry and ExpenseEntry.

These are the primary data inputs that drive the health engine.
Every write triggers a score recalculation for the tenant.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID, uuid4


class PaymentMethod(StrEnum):
    CASH = "cash"
    DEBIT_CARD = "debit_card"
    CREDIT_CARD = "credit_card"
    TRANSFER = "transfer"
    QR = "qr"
    OTHER = "other"


class ExpenseCategory(StrEnum):
    RENT = "rent"
    UTILITIES = "utilities"
    SALARIES = "salaries"
    SUPPLIERS = "suppliers"
    TAXES = "taxes"
    MARKETING = "marketing"
    MAINTENANCE = "maintenance"
    FINANCING = "financing"
    OTHER = "other"


@dataclass
class SaleEntry:
    """A single sales transaction registered by the tenant."""

    tenant_id: UUID
    amount: Decimal
    quantity: int
    transaction_date: date
    payment_method: PaymentMethod
    product_id: UUID | None = None
    notes: str | None = None
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def __post_init__(self) -> None:
        if self.amount <= Decimal("0"):
            raise ValueError("Sale amount must be positive.")
        if self.quantity <= 0:
            raise ValueError("Sale quantity must be positive.")

    @property
    def unit_price(self) -> Decimal:
        return self.amount / self.quantity


@dataclass
class ExpenseEntry:
    """A single expense registered by the tenant."""

    tenant_id: UUID
    amount: Decimal
    category: ExpenseCategory
    transaction_date: date
    description: str
    is_recurring: bool = False
    payment_method: PaymentMethod = PaymentMethod.TRANSFER
    supplier_name: str | None = None
    notes: str | None = None
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def __post_init__(self) -> None:
        if self.amount <= Decimal("0"):
            raise ValueError("Expense amount must be positive.")
