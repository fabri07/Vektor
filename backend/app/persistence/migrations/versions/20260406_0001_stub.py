"""Stub for removed Sprint 1 Google Workspace migration.

Revision ID: 20260406_0001
Revises: 20260401_0003
Create Date: 2026-04-06

The original migration was removed when the bespoke Google Workspace
integration was replaced by MCP-based integrations. Some databases were
already stamped with this revision, so the repository keeps a no-op
compatibility stub to preserve Alembic continuity.
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "20260406_0001"
down_revision: str | None = "20260401_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
