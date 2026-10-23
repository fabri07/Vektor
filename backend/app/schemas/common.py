"""
Shared Pydantic schemas and response wrappers.
"""

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict

T = TypeVar("T")


class CamelModel(BaseModel):
    """Base model with camelCase serialization for API responses."""

    model_config = ConfigDict(
        populate_by_name=True,
        from_attributes=True,
    )


class PaginatedResponse(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        return (self.offset + self.limit) < self.total


class MessageResponse(BaseModel):
    message: str


class ErrorDetail(BaseModel):
    code: str
    message: str
    field: str | None = None


class ErrorResponse(BaseModel):
    detail: str | list[ErrorDetail]
