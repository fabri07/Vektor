"""Durable cross-session chat memory for data loading events."""

from __future__ import annotations

import json
import uuid
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.observability.logger import get_logger
from app.persistence.models.chat_session_log import ChatSessionLog

logger = get_logger(__name__)


class ChatMemoryService:
    CACHE_TTL = 300

    def _cache_key(self, tenant_id: uuid.UUID) -> str:
        return f"chat_ctx:{tenant_id}"

    async def record(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        session_id: str,
        entry_type: str,
        description: str,
        entity_type: str | None = None,
        entity_count: int = 0,
        period_start: date | None = None,
        period_end: date | None = None,
        pending_action_id: uuid.UUID | None = None,
        uploaded_file_id: uuid.UUID | None = None,
        redis: Any | None = None,
    ) -> ChatSessionLog:
        log = ChatSessionLog(
            tenant_id=tenant_id,
            session_id=session_id[:128],
            entry_type=entry_type,
            description=description,
            entity_type=entity_type,
            entity_count=entity_count,
            period_start=period_start,
            period_end=period_end,
            pending_action_id=pending_action_id,
            uploaded_file_id=uploaded_file_id,
        )
        db.add(log)
        await db.flush()

        if redis is not None:
            try:
                await redis.delete(self._cache_key(tenant_id))
            except Exception as exc:
                logger.warning("chat_memory.cache_invalidate_failed", error=str(exc))

        return log

    async def get_session_context(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        limit: int = 10,
    ) -> dict[str, Any]:
        stmt = (
            select(ChatSessionLog)
            .where(ChatSessionLog.tenant_id == tenant_id)
            .order_by(ChatSessionLog.created_at.desc())
            .limit(limit)
        )
        result = await db.execute(stmt)
        logs = list(result.scalars().all())

        count_stmt = select(func.count(ChatSessionLog.id)).where(
            ChatSessionLog.tenant_id == tenant_id,
            ChatSessionLog.entry_type == "DATA_LOADED",
        )
        total_cargas = int((await db.scalar(count_stmt)) or 0)

        eventos = [self._serialize_log(log) for log in logs]
        ultima_carga = next(
            (item for item in eventos if item["entry_type"] == "DATA_LOADED"),
            None,
        )

        return {
            "historial_disponible": bool(logs),
            "ultima_carga": ultima_carga,
            "total_cargas_registradas": total_cargas,
            "eventos_recientes": eventos,
        }

    async def get_session_context_cached(
        self,
        db: AsyncSession,
        redis: Any,
        tenant_id: uuid.UUID,
        limit: int = 10,
    ) -> dict[str, Any]:
        key = self._cache_key(tenant_id)
        try:
            cached = await redis.get(key)
            if cached:
                raw = cached.decode() if isinstance(cached, bytes) else cached
                return json.loads(raw)
        except Exception as exc:
            logger.warning("chat_memory.cache_get_failed", error=str(exc))

        ctx = await self.get_session_context(db, tenant_id, limit=limit)
        try:
            await redis.setex(key, self.CACHE_TTL, json.dumps(ctx, default=str))
        except Exception as exc:
            logger.warning("chat_memory.cache_set_failed", error=str(exc))
        return ctx

    @staticmethod
    def _serialize_log(log: ChatSessionLog) -> dict[str, Any]:
        return {
            "id": str(log.id),
            "session_id": log.session_id,
            "entry_type": log.entry_type,
            "descripcion": log.description,
            "entity_type": log.entity_type,
            "registros": log.entity_count,
            "period_start": log.period_start.isoformat() if log.period_start else None,
            "period_end": log.period_end.isoformat() if log.period_end else None,
            "pending_action_id": str(log.pending_action_id) if log.pending_action_id else None,
            "uploaded_file_id": str(log.uploaded_file_id) if log.uploaded_file_id else None,
            "fecha": log.created_at.isoformat() if log.created_at else None,
        }
