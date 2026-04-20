"""BusinessMemoryService — Capa 3 de memoria: estado del negocio.

Redis (TTL 5 min) como cache caliente + PostgreSQL como persistencia.
Todas las operaciones son fail-silencioso: nunca bloquean el request principal.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.anthropic_client import get_anthropic_async_client
from app.observability.logger import get_logger
from app.persistence.models.memory import BusinessMemory

logger = get_logger(__name__)

_REDIS_KEY = "biz_mem:{tenant_id}"
_REDIS_TTL = 300  # 5 minutos


class BusinessMemoryService:
    def __init__(self, db: AsyncSession, redis: Redis) -> None:
        self._db = db
        self._redis = redis

    async def get(self, tenant_id: uuid.UUID) -> dict[str, Any]:
        """Redis primero, fallback a PostgreSQL. Nunca lanza excepción."""
        try:
            raw = await self._redis.get(_REDIS_KEY.format(tenant_id=tenant_id))
            if raw:
                return json.loads(raw)
        except Exception:
            logger.warning("biz_mem.redis_miss", tenant_id=str(tenant_id))

        try:
            res = await self._db.execute(
                select(BusinessMemory).where(BusinessMemory.tenant_id == tenant_id)
            )
            mem = res.scalar_one_or_none()
            if mem is None:
                return self._empty()
            data = self._to_dict(mem)
            await self._set_cache(tenant_id, data)
            return data
        except Exception:
            logger.warning("biz_mem.db_miss", tenant_id=str(tenant_id))
            try:
                await self._db.rollback()
            except Exception:
                pass
            return self._empty()

    async def update_after_sale(self, tenant_id: uuid.UUID, amount: Decimal) -> None:
        """Actualiza acumulados de ventas y last_sale. Fail-silencioso."""
        try:
            res = await self._db.execute(
                select(BusinessMemory).where(BusinessMemory.tenant_id == tenant_id)
            )
            mem = res.scalar_one_or_none()
            now = datetime.now(timezone.utc)
            if mem is None:
                mem = BusinessMemory(
                    tenant_id=tenant_id,
                    sales_today=amount,
                    sales_this_week=amount,
                    sales_this_month=amount,
                    last_sale_amount=amount,
                    last_sale_at=now,
                )
                self._db.add(mem)
            else:
                mem.sales_today = (mem.sales_today or Decimal("0")) + amount
                mem.sales_this_week = (mem.sales_this_week or Decimal("0")) + amount
                mem.sales_this_month = (mem.sales_this_month or Decimal("0")) + amount
                mem.last_sale_amount = amount
                mem.last_sale_at = now
                mem.updated_at = now
            await self._db.flush()
            await self._invalidate(tenant_id)
        except Exception:
            logger.warning("biz_mem.update_sale_failed", tenant_id=str(tenant_id))

    async def update_after_expense(
        self, tenant_id: uuid.UUID, amount: Decimal, category: str = ""
    ) -> None:
        """Actualiza expenses_today. Fail-silencioso."""
        try:
            res = await self._db.execute(
                select(BusinessMemory).where(BusinessMemory.tenant_id == tenant_id)
            )
            mem = res.scalar_one_or_none()
            now = datetime.now(timezone.utc)
            if mem is None:
                mem = BusinessMemory(tenant_id=tenant_id, expenses_today=amount)
                self._db.add(mem)
            else:
                mem.expenses_today = (mem.expenses_today or Decimal("0")) + amount
                mem.updated_at = now
            await self._db.flush()
            await self._invalidate(tenant_id)
        except Exception:
            logger.warning(
                "biz_mem.update_expense_failed",
                tenant_id=str(tenant_id),
                category=category,
            )

    async def generate_and_save_summary(self, tenant_id: uuid.UUID) -> None:
        """Genera llm_context_summary con Haiku y persiste. Fail-silencioso."""
        try:
            data = await self.get(tenant_id)
            summary = await self._generate_summary(data)
            if not summary:
                return
            res = await self._db.execute(
                select(BusinessMemory).where(BusinessMemory.tenant_id == tenant_id)
            )
            mem = res.scalar_one_or_none()
            if mem:
                mem.llm_context_summary = summary
                mem.llm_context_updated_at = datetime.now(timezone.utc)
                await self._db.flush()
            await self._invalidate(tenant_id)
        except Exception:
            logger.warning("biz_mem.summary_gen_failed", tenant_id=str(tenant_id))

    async def _generate_summary(self, memory: dict[str, Any]) -> str:
        try:
            client = get_anthropic_async_client()
            resp = await client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=80,
                system=(
                    "Resumí el estado financiero del negocio en 1-2 frases cortas "
                    "en español argentino."
                ),
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"Ventas hoy: ${float(memory.get('sales_today', 0)):.0f}. "
                            f"Gastos hoy: ${float(memory.get('expenses_today', 0)):.0f}. "
                            f"Ventas este mes: ${float(memory.get('sales_this_month', 0)):.0f}."
                        ),
                    }
                ],
            )
            return resp.content[0].text.strip()
        except Exception:
            return ""

    async def _set_cache(self, tenant_id: uuid.UUID, data: dict[str, Any]) -> None:
        try:
            await self._redis.setex(
                _REDIS_KEY.format(tenant_id=tenant_id), _REDIS_TTL, json.dumps(data)
            )
        except Exception:
            pass

    async def _invalidate(self, tenant_id: uuid.UUID) -> None:
        try:
            await self._redis.delete(_REDIS_KEY.format(tenant_id=tenant_id))
        except Exception:
            pass

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "sales_today": 0,
            "sales_this_week": 0,
            "sales_this_month": 0,
            "expenses_today": 0,
            "products_in_stockout": 0,
            "llm_context_summary": None,
        }

    @staticmethod
    def _to_dict(mem: BusinessMemory) -> dict[str, Any]:
        return {
            "sales_today": float(mem.sales_today or 0),
            "sales_this_week": float(mem.sales_this_week or 0),
            "sales_this_month": float(mem.sales_this_month or 0),
            "expenses_today": float(mem.expenses_today or 0),
            "products_in_stockout": mem.products_in_stockout or 0,
            "llm_context_summary": mem.llm_context_summary,
        }
