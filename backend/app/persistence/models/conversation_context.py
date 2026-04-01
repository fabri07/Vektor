"""ORM model: agent_conversation_context.

Guarda el historial de chat por conversación.
Redis es el cache caliente; PostgreSQL es el backup persistente.
Estrategia: sliding window de 10 turnos + summarización al superar 8.000 tokens.
"""

import uuid
from typing import Any

from sqlalchemy import ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db.base import PGJSONB, Base, TimestampMixin


class AgentConversationContext(TimestampMixin, Base):
    __tablename__ = "agent_conversation_context"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    turns: Mapped[list[Any]] = mapped_column(PGJSONB, nullable=False, default=list)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    def __repr__(self) -> str:
        return (
            f"<AgentConversationContext id={self.conversation_id} tenant={self.tenant_id}>"
        )
