"""Command: CreateTenant."""

from dataclasses import dataclass


@dataclass(frozen=True)
class CreateTenantCommand:
    name: str
    slug: str
    vertical: str
    owner_email: str
    owner_full_name: str
    owner_password: str
