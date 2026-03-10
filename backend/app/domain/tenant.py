"""
Tenant domain entity and value objects.

Pure Python — no framework dependencies.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4


class TenantStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    CANCELLED = "cancelled"
    TRIAL = "trial"


class BusinessVertical(StrEnum):
    KIOSCO = "kiosco"
    DECORACION_HOGAR = "decoracion_hogar"
    LIMPIEZA = "limpieza"


@dataclass(frozen=True)
class TenantId:
    value: UUID

    @classmethod
    def generate(cls) -> "TenantId":
        return cls(value=uuid4())

    def __str__(self) -> str:
        return str(self.value)


@dataclass
class Tenant:
    """Core tenant aggregate root."""

    name: str
    slug: str
    vertical: BusinessVertical
    id: TenantId = field(default_factory=TenantId.generate)
    status: TenantStatus = TenantStatus.TRIAL
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def suspend(self) -> None:
        if self.status == TenantStatus.CANCELLED:
            raise ValueError("Cannot suspend a cancelled tenant.")
        self.status = TenantStatus.SUSPENDED
        self.updated_at = datetime.utcnow()

    def activate(self) -> None:
        self.status = TenantStatus.ACTIVE
        self.updated_at = datetime.utcnow()

    def cancel(self) -> None:
        self.status = TenantStatus.CANCELLED
        self.updated_at = datetime.utcnow()

    @property
    def is_active(self) -> bool:
        return self.status in (TenantStatus.ACTIVE, TenantStatus.TRIAL)
