"""ConversationService — gestión de contexto de conversación agente.

Estrategia:
  - Redis como cache caliente (TTL 24 h).
  - PostgreSQL como backup persistente.
  - Sliding window de 10 turnos; trunca automáticamente al superar ese límite.
  - Summarización prevista cuando total_tokens supera MAX_TOKENS_BEFORE_SUMMARIZE.

Una conversación es de su negocio (X6-2, revisión profunda 2026-10-26): el
``conversation_id`` lo manda el cliente, así que todo acceso recibe el
``tenant_id`` del JWT. La clave de Redis lleva el tenant (``conv:{tenant}:{id}``)
y una fila de PostgreSQL de OTRO tenant se trata como inexistente: no se lee
como contexto y ``persist`` no la pisa. Las claves viejas ``conv:{id}`` no se
leen más y vencen solas por TTL.
"""

import json
import uuid
from datetime import UTC, datetime
from typing import Any, cast

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.observability.logger import get_logger
from app.persistence.models.conversation_context import AgentConversationContext

REDIS_TTL = 86400  # 24 horas
MAX_TOKENS_BEFORE_SUMMARIZE = 8000
MAX_TURNS = 10

# Vida útil del puntero a la última acción pendiente confirmable por lenguaje
# natural. Pasado este lapso, "sí" ya no confirma (la acción se considera vieja).
PENDING_CONFIRM_TTL_SECONDS = 600  # 10 minutos

logger = get_logger(__name__)


def conversation_key(conversation_id: str, tenant_id: uuid.UUID) -> str:
    """Clave de Redis del contexto: con el tenant adentro (X6-2)."""
    return f"conv:{tenant_id}:{conversation_id}"


class ConversationService:
    def __init__(self, redis_client: Redis, db_session: AsyncSession) -> None:
        self.redis = redis_client
        self.db = db_session

    async def get_context(self, conversation_id: str, *, tenant_id: uuid.UUID) -> dict[str, Any]:
        """Obtener contexto: primero desde Redis, fallback a PostgreSQL.

        Una fila de otro tenant es, para este negocio, una conversación que no
        existe: se devuelve el contexto vacío (X6-2).
        """
        key = conversation_key(conversation_id, tenant_id)
        try:
            cached = await self.redis.get(key)
            if cached:
                return cast("dict[str, Any]", json.loads(cached))
        except Exception as exc:
            logger.warning("redis_get_failed", conversation_id=conversation_id, error=str(exc))

        ctx = await self.db.get(AgentConversationContext, uuid.UUID(conversation_id))
        if ctx is not None and ctx.tenant_id != tenant_id:
            logger.warning(
                "conversation_de_otro_negocio",
                conversation_id=conversation_id,
                tenant_id=str(tenant_id),
            )
            ctx = None
        if ctx:
            data = {"turns": ctx.turns, "summary": ctx.summary}
            try:
                await self.redis.setex(key, REDIS_TTL, json.dumps(data))
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
        *,
        tenant_id: uuid.UUID,
    ) -> dict[str, Any]:
        """Agregar un turno al historial. Mantiene los últimos MAX_TURNS turnos."""
        ctx = await self.get_context(conversation_id, tenant_id=tenant_id)
        turn: dict[str, Any] = {"role": role, "content": content}
        if metadata:
            turn.update(metadata)
        ctx["turns"].append(turn)

        if len(ctx["turns"]) > MAX_TURNS:
            ctx["turns"] = ctx["turns"][-MAX_TURNS:]

        await self.redis.setex(
            conversation_key(conversation_id, tenant_id), REDIS_TTL, json.dumps(ctx)
        )
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
        ctx = await self.get_context(conversation_id, tenant_id=tenant_id)
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
            await self.redis.setex(
                conversation_key(conversation_id, tenant_id), REDIS_TTL, json.dumps(ctx)
            )
        except Exception as exc:
            logger.warning("redis_write_failed", conversation_id=conversation_id, error=str(exc))
        await self.persist(conversation_id, tenant_id, user_id)
        await self.db.commit()
        return ctx

    # ── Puntero a la última acción pendiente (confirmación por lenguaje natural) ──

    async def record_pending_action(
        self,
        conversation_id: str,
        *,
        tenant_id: uuid.UUID,
        pending_action_id: str | None,
        approval_group_id: str | None,
        status: str,
    ) -> None:
        """Guarda en el contexto el puntero a la última acción/grupo pendiente.

        Se invoca cuando una respuesta sale ``requires_approval`` para que el
        router de confirmación por lenguaje natural pueda ejecutar/cancelar en el
        turno siguiente. Solo Redis (puntero volátil, TTL-bound). Fail-silencioso.
        """
        try:
            ctx = await self.get_context(conversation_id, tenant_id=tenant_id)
            ctx["pending"] = {
                "pending_action_id": pending_action_id,
                "approval_group_id": approval_group_id,
                "status": status,
                "ts": datetime.now(UTC).isoformat(),
            }
            await self.redis.setex(
                conversation_key(conversation_id, tenant_id), REDIS_TTL, json.dumps(ctx)
            )
        except Exception as exc:
            logger.warning(
                "conversation_record_pending_failed",
                conversation_id=conversation_id,
                error=str(exc),
            )

    async def get_pending_action(
        self, conversation_id: str, *, tenant_id: uuid.UUID
    ) -> dict[str, Any] | None:
        """Devuelve el puntero a la acción pendiente viva (no expirado), o None.

        Considera expirado todo puntero más viejo que ``PENDING_CONFIRM_TTL_SECONDS``.
        """
        try:
            ctx = await self.get_context(conversation_id, tenant_id=tenant_id)
        except Exception as exc:
            logger.warning(
                "conversation_get_pending_failed",
                conversation_id=conversation_id,
                error=str(exc),
            )
            return None
        pending = ctx.get("pending")
        if not isinstance(pending, dict):
            return None
        ts_raw = pending.get("ts")
        if not ts_raw:
            return None
        try:
            ts = datetime.fromisoformat(str(ts_raw))
        except ValueError:
            return None
        if (datetime.now(UTC) - ts).total_seconds() > PENDING_CONFIRM_TTL_SECONDS:
            return None
        return pending

    async def clear_pending_action(self, conversation_id: str, *, tenant_id: uuid.UUID) -> None:
        """Limpia el puntero a la acción pendiente (tras confirmar/cancelar)."""
        try:
            ctx = await self.get_context(conversation_id, tenant_id=tenant_id)
            if "pending" in ctx:
                ctx.pop("pending", None)
                await self.redis.setex(
                    conversation_key(conversation_id, tenant_id), REDIS_TTL, json.dumps(ctx)
                )
        except Exception as exc:
            logger.warning(
                "conversation_clear_pending_failed",
                conversation_id=conversation_id,
                error=str(exc),
            )

    async def persist(
        self,
        conversation_id: str,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> AgentConversationContext | None:
        """Persistir el contexto de Redis a PostgreSQL.

        Si la fila existe y es de OTRO tenant no se toca y devuelve None (X6-2):
        el turno queda sólo en la clave de Redis de este negocio. No se rechaza
        el mensaje — el cliente no tiene cómo elegir otra conversación a mitad
        de un turno —, pero tampoco se escribe en la conversación ajena.
        """
        ctx_data = await self.get_context(conversation_id, tenant_id=tenant_id)
        conv_uuid = uuid.UUID(conversation_id)

        existing = await self.db.get(AgentConversationContext, conv_uuid)
        if existing is not None and existing.tenant_id != tenant_id:
            logger.warning(
                "conversation_persist_de_otro_negocio",
                conversation_id=conversation_id,
                tenant_id=str(tenant_id),
            )
            return None
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
