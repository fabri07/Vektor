"""ConversationService — gestión de contexto de conversación agente.

Estrategia:
  - Redis como cache caliente (TTL 24 h).
  - PostgreSQL como backup persistente.
  - Sliding window de 10 turnos; trunca automáticamente al superar ese límite.
  - Summarización prevista cuando total_tokens supera MAX_TOKENS_BEFORE_SUMMARIZE.
"""

import json
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.conversation_context import AgentConversationContext

REDIS_TTL = 86400  # 24 horas
MAX_TOKENS_BEFORE_SUMMARIZE = 8000
MAX_TURNS = 10


class ConversationService:
    def __init__(self, redis_client, db_session: AsyncSession) -> None:
        self.redis = redis_client
        self.db = db_session

    async def get_context(self, conversation_id: str) -> dict:
        """Obtener contexto: primero desde Redis, fallback a PostgreSQL."""
        cached = await self.redis.get(f"conv:{conversation_id}")
        if cached:
            return json.loads(cached)

        ctx = await self.db.get(AgentConversationContext, uuid.UUID(conversation_id))
        if ctx:
            data = {"turns": ctx.turns, "summary": ctx.summary}
            await self.redis.setex(
                f"conv:{conversation_id}", REDIS_TTL, json.dumps(data)
            )
            return data

        return {"turns": [], "summary": None}

    async def add_turn(
        self, conversation_id: str, role: str, content: str, tokens: int = 200
    ) -> dict:
        """Agregar un turno al historial. Mantiene los últimos MAX_TURNS turnos."""
        ctx = await self.get_context(conversation_id)
        ctx["turns"].append({"role": role, "content": content})

        if len(ctx["turns"]) > MAX_TURNS:
            ctx["turns"] = ctx["turns"][-MAX_TURNS:]

        await self.redis.setex(
            f"conv:{conversation_id}", REDIS_TTL, json.dumps(ctx)
        )
        return ctx

    async def persist(
        self,
        conversation_id: str,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> AgentConversationContext:
        """Persistir el contexto de Redis a PostgreSQL."""
        ctx_data = await self.get_context(conversation_id)
        conv_uuid = uuid.UUID(conversation_id)

        existing = await self.db.get(AgentConversationContext, conv_uuid)
        if existing:
            existing.turns = ctx_data["turns"]
            existing.summary = ctx_data["summary"]
            self.db.add(existing)
            return existing

        record = AgentConversationContext(
            conversation_id=conv_uuid,
            tenant_id=tenant_id,
            user_id=user_id,
            turns=ctx_data["turns"],
            summary=ctx_data["summary"],
        )
        self.db.add(record)
        return record
