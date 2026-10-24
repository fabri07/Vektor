"""
Transaction domain entities: SaleEntry and ExpenseEntry.

These are the primary data inputs that drive the health engine.
Every write triggers a score recalculation for the tenant.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID, uuid4


class PaymentMethod(StrEnum):
    CASH = "cash"
    DEBIT_CARD = "debit_card"
    CREDIT_CARD = "credit_card"
    TRANSFER = "transfer"
    QR = "qr"
    # Cuenta corriente / fiado: señal real en kioscos (venta sin plata en caja,
    # gasto a pagar al proveedor). No es "other" — se reporta por separado.
    ACCOUNT = "account"
    OTHER = "other"


#: Marcador de las líneas de una operación de caja pagada con más de un método
#: distinto (B9). NO es un medio de pago: queda fuera de ``PaymentMethod`` y de
#: ``PAYMENT_METHOD_PATTERN`` a propósito, para que ningún cliente pueda
#: declararlo. Lo escribe sólo el servidor, y el importe de cada medio de esas
#: ventas vive en ``pos_tenders`` (ver ``repositories/_pos_tender_scope.py``).
MIXED_PAYMENT_METHOD = "mixed"

#: Cobro de una venta fiada: una fila de ``sales_entries`` con este método (fuera
#: de ``PaymentMethod``, lo escribe ``cash_service.save_cash_inflow``). NO es una
#: venta —la venta fiada ya se contó como ingreso cuando se hizo—: es plata que
#: entra a caja y baja la deuda del cliente. Todo lector de plata la separa con
#: ``repositories/_ventas_scope.py``.
INFLOW_PAYMENT_METHOD = "inflow"


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
    transaction_date: datetime
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
    transaction_date: datetime
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
