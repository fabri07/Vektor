"""
User domain entity and RBAC value objects.

Pure Python — no framework dependencies.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4


class UserRole(StrEnum):
    OWNER = "owner"       # Dueño del negocio — acceso total al tenant
    ADMIN = "admin"       # Administrador — acceso total salvo billing
    ANALYST = "analyst"   # Solo lectura de reportes y scores
    VIEWER = "viewer"     # Solo lectura del dashboard


class UserStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    PENDING_VERIFICATION = "pending_verification"


@dataclass(frozen=True)
class UserId:
    value: UUID

    @classmethod
    def generate(cls) -> "UserId":
        return cls(value=uuid4())

    def __str__(self) -> str:
        return str(self.value)


@dataclass(frozen=True)
class Email:
    value: str

    def __post_init__(self) -> None:
        if "@" not in self.value or "." not in self.value.split("@")[-1]:
            raise ValueError(f"Invalid email address: {self.value!r}")

    def __str__(self) -> str:
        return self.value


@dataclass
class User:
    """User aggregate — belongs to exactly one Tenant."""

    tenant_id: UUID
    email: Email
    full_name: str
    hashed_password: str
    role: UserRole
    id: UserId = field(default_factory=UserId.generate)
    status: UserStatus = UserStatus.PENDING_VERIFICATION
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    last_login_at: datetime | None = None

    def activate(self) -> None:
        self.status = UserStatus.ACTIVE
        self.updated_at = datetime.utcnow()

    def deactivate(self) -> None:
        self.status = UserStatus.INACTIVE
        self.updated_at = datetime.utcnow()

    def record_login(self) -> None:
        self.last_login_at = datetime.utcnow()
        self.updated_at = datetime.utcnow()

    def can(self, permission: str) -> bool:
        """Simple RBAC permission check."""
        _permissions: dict[UserRole, frozenset[str]] = {
            UserRole.OWNER: frozenset({"read", "write", "delete", "billing", "admin"}),
            UserRole.ADMIN: frozenset({"read", "write", "delete"}),
            UserRole.ANALYST: frozenset({"read"}),
            UserRole.VIEWER: frozenset({"read"}),
        }
        return permission in _permissions.get(self.role, frozenset())

    @property
    def is_active(self) -> bool:
        return self.status == UserStatus.ACTIVE
