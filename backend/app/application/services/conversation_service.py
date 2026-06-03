"""ConversationService — gestión de contexto de conversación agente.

Estrategia:
  - Redis como cache caliente (TTL 24 h).
  - PostgreSQL como backup persistente.
  - Sliding window de 10 turnos; trunca automáticamente al superar ese límite.
  - Summarización prevista cuando total_tokens supera MAX_TOKENS_BEFORE_SUMMARIZE.
"""

import json
import uuid
from typing import Any, cast

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.observability.logger import get_logger
from app.persistence.models.conversation_context import AgentConversationContext

REDIS_TTL = 86400  # 24 horas
MAX_TOKENS_BEFORE_SUMMARIZE = 8000
MAX_TURNS = 10

logger = get_logger(__name__)


class ConversationService:
    def __init__(self, redis_client: Redis, db_session: AsyncSession) -> None:
        self.redis = redis_client
        self.db = db_session

    async def get_context(self, conversation_id: str) -> dict[str, Any]:
        """Obtener contexto: primero desde Redis, fallback a PostgreSQL."""
        try:
            cached = await self.redis.get(f"conv:{conversation_id}")
            if cached:
                return cast("dict[str, Any]", json.loads(cached))
        except Exception as exc:
            logger.warning("redis_get_failed", conversation_id=conversation_id, error=str(exc))

        ctx = await self.db.get(AgentConversationContext, uuid.UUID(conversation_id))
        if ctx:
            data = {"turns": ctx.turns, "summary": ctx.summary}
            try:
                await self.redis.setex(f"conv:{conversation_id}", REDIS_TTL, json.dumps(data))
            except Exception as exc:
                logger.warning(
                    "redis_rehydrate_failed", conversation_id=conversation_id, error=str(exc)
                )
            return data

        return {"turns": [], "summary": None}

    async def add_turn(
        self,
        conversation_id: str,
        role: str,
        content: str,
        tokens: int = 200,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Agregar un turno al historial. Mantiene los últimos MAX_TURNS turnos."""
        ctx = await self.get_context(conversation_id)
        turn: dict[str, Any] = {"role": role, "content": content}
        if metadata:
            turn.update(metadata)
        ctx["turns"].append(turn)

        if len(ctx["turns"]) > MAX_TURNS:
            ctx["turns"] = ctx["turns"][-MAX_TURNS:]

        await self.redis.setex(f"conv:{conversation_id}", REDIS_TTL, json.dumps(ctx))
        return ctx

    async def _summarize_turns(self, turns: list[dict[str, Any]]) -> str:
        """Resume turnos viejos con Haiku. Fail-silencioso."""
        try:
            from app.integrations.anthropic_client import (
                get_anthropic_async_client,  # noqa: PLC0415
            )

            client: Any = get_anthropic_async_client()
            resp = await client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=80,
                system="Resumí esta conversación en máximo 2 frases en español.",
                messages=[
                    {
                        "role": "user",
                        "content": json.dumps(turns, ensure_ascii=False),
                    }
                ],
            )
            return cast("str", resp.content[0].text.strip())
        except Exception as exc:
            logger.warning("conversation_summarize_failed", error=str(exc))
            return ""

    async def add_turn_with_summary(
        self,
        conversation_id: str,
        role: str,
        content: str,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Agrega turno con summarización automática al superar MAX_TURNS."""
        ctx = await self.get_context(conversation_id)
        turn: dict[str, Any] = {"role": role, "content": content}
        if metadata:
            turn.update(metadata)
        ctx["turns"].append(turn)

        if len(ctx["turns"]) > MAX_TURNS:
            summary = await self._summarize_turns(ctx["turns"][:-5])
            ctx["turns"] = ctx["turns"][-5:]
            if summary:
                ctx["summary"] = summary

        try:
            await self.redis.setex(f"conv:{conversation_id}", REDIS_TTL, json.dumps(ctx))
        except Exception as exc:
            logger.warning("redis_write_failed", conversation_id=conversation_id, error=str(exc))
        await self.persist(conversation_id, tenant_id, user_id)
        await self.db.commit()
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
