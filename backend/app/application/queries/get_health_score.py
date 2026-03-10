"""Query: GetHealthScore."""

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class GetHealthScoreQuery:
    tenant_id: UUID


@dataclass(frozen=True)
class GetHealthScoreHistoryQuery:
    tenant_id: UUID
    limit: int = 30
