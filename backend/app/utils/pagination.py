"""Pagination helpers."""

from dataclasses import dataclass


@dataclass
class PaginationParams:
    limit: int = 50
    offset: int = 0

    def __post_init__(self) -> None:
        self.limit = max(1, min(self.limit, 200))
        self.offset = max(0, self.offset)

    @property
    def next_offset(self) -> int:
        return self.offset + self.limit
