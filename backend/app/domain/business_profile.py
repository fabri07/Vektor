"""
BusinessProfile domain entity.

Represents the static profile of a PYME: sector, size, location, etc.
Changes here trigger a score recalculation via Business State Layer.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID, uuid4


class BusinessSize(StrEnum):
    MICRO = "micro"       # 1–5 empleados
    SMALL = "small"       # 6–25 empleados
    MEDIUM = "medium"     # 26–100 empleados


@dataclass
class BusinessProfile:
    """Core profile of a tenant's business."""

    tenant_id: UUID
    legal_name: str
    trade_name: str
    cuit: str                        # Argentinian tax ID
    vertical: str
    size: BusinessSize
    province: str
    city: str
    employee_count: int
    monthly_rent: Decimal | None = None
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def update_employee_count(self, count: int) -> None:
        if count < 0:
            raise ValueError("Employee count cannot be negative.")
        self.employee_count = count
        self.updated_at = datetime.utcnow()

    def update_rent(self, amount: Decimal) -> None:
        if amount < Decimal("0"):
            raise ValueError("Monthly rent cannot be negative.")
        self.monthly_rent = amount
        self.updated_at = datetime.utcnow()

    @property
    def has_rent(self) -> bool:
        return self.monthly_rent is not None and self.monthly_rent > Decimal("0")
