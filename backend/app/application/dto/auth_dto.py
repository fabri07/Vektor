"""Data Transfer Objects for Auth application layer."""

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class AuthenticatedUserDTO:
    user_id: UUID
    tenant_id: UUID
    email: str
    role: str
    tenant_status: str
