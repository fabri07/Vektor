"""AgentMemoryService — Memoria permanente de patrones aprendidos por agente/tenant.

Detecta y persiste patrones de comportamiento por tenant:
  - método de pago preferido
  - monto promedio de venta
  - tipos de acción más frecuentes
  - categorías de gasto más comunes

Redis (TTL 5 min) como cache caliente + PostgreSQL como persistencia.
Todas las operaciones son fail-silencioso.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.observability.logger import get_logger
from app.persistence.models.memory import AgentMemory

logger = get_logger(__name__)

_REDIS_KEY = "agent_mem:{tenant_id}"
_REDIS_TTL = 300  # 5 minutos

# Claves de patrones que este servicio gestiona
_PATTERN_KEYS = {
    "preferred_payment_method",
    "avg_sale_amount",
    "top_action_types",
    "common_expense_categories",
}


class AgentMemoryService:
    def __init__(self, db: AsyncSession, redis: Redis) -> None:
        self._db = db
        self._redis = redis

    # ── API pública ───────────────────────────────────────────────────────────

    async def get(self, tenant_id: uuid.UUID) -> dict[str, Any]:
        """Retorna todos los patrones del tenant. Redis primero, fallback a DB."""
        try:
            raw = await self._redis.get(_REDIS_KEY.format(tenant_id=tenant_id))
            if raw:
                return json.loads(raw)  # type: ignore[no-any-return]
        except Exception:
            logger.warning("agent_mem.redis_miss", tenant_id=str(tenant_id))

        return await self._load_from_db(tenant_id)

    async def get_context_fragment(self, tenant_id: uuid.UUID) -> str:
        """Retorna un fragmento de texto listo para inyectar en system prompts."""
        try:
            patterns = await self.get(tenant_id)
            if not patterns:
                return ""
            lines: list[str] = ["Patrones aprendidos del negocio:"]
            pm = patterns.get("preferred_payment_method")
            if pm and pm.get("method"):
                pct = pm.get("pct", 0)
                lines.append(
                    f"- Método de pago más frecuente: {pm['method']} ({pct:.0f}% de ventas)"
                )
            avg = patterns.get("avg_sale_amount")
            if avg and avg.get("value"):
                lines.append(f"- Monto promedio de venta: ${avg['value']:.0f}")
            cats = patterns.get("common_expense_categories")
            if cats and cats.get("top"):
                top = cats["top"][:2]
                lines.append(f"- Categorías de gasto más frecuentes: {', '.join(top)}")
            top_actions = patterns.get("top_action_types")
            if top_actions and top_actions.get("top"):
                lines.append(f"- Acciones más usadas: {', '.join(top_actions['top'][:3])}")
            return "\n".join(lines) if len(lines) > 1 else ""
        except Exception:
            logger.warning("agent_mem.context_fragment_failed", tenant_id=str(tenant_id))
            return ""

    async def record_action(
        self,
        tenant_id: uuid.UUID,
        action_type: str,
        payload: dict[str, Any],
    ) -> None:
        """Registra una acción ejecutada y actualiza los patrones relevantes. Fail-silencioso."""
        try:
            await self._update_top_action_types(tenant_id, action_type)
            if action_type == "REGISTER_SALE":
                await self._update_payment_method(tenant_id, payload)
                await self._update_avg_sale_amount(tenant_id, payload)
            elif action_type in (
                "REGISTER_EXPENSE",
                "REGISTER_PURCHASE",
                "REGISTER_CASH_OUTFLOW",
            ):
                await self._update_expense_categories(tenant_id, payload)
            await self._invalidate(tenant_id)
        except Exception:
            logger.warning(
                "agent_mem.record_action_failed",
                tenant_id=str(tenant_id),
                action_type=action_type,
            )

    # ── Actualizadores de patrones ────────────────────────────────────────────

    async def _update_payment_method(self, tenant_id: uuid.UUID, payload: dict[str, Any]) -> None:
        method = payload.get("payment_method") or payload.get("payment_status")
        if not method:
            return
        current = await self._get_pattern(tenant_id, "preferred_payment_method")
        counts: dict[str, int] = current.get("counts", {})
        counts[str(method)] = counts.get(str(method), 0) + 1
        total = sum(counts.values())
        top_method = max(counts, key=lambda k: counts[k])
        await self._upsert_pattern(
            tenant_id,
            "preferred_payment_method",
            {
                "method": top_method,
                "pct": (counts[top_method] / total) * 100,
                "counts": counts,
            },
        )

    async def _update_avg_sale_amount(
        self, tenant_id: uuid.UUID, payload: dict[str, Any]
    ) -> None:
        amount = payload.get("amount")
        if not amount:
            return
        try:
            amount_f = float(amount)
        except (TypeError, ValueError):
            return
        current = await self._get_pattern(tenant_id, "avg_sale_amount")
        n = current.get("n", 0)
        prev_avg = current.get("value", 0.0)
        new_n = n + 1
        new_avg = prev_avg + (amount_f - prev_avg) / new_n  # Welford online mean
        await self._upsert_pattern(
            tenant_id,
            "avg_sale_amount",
            {"value": round(new_avg, 2), "n": new_n},
        )

    async def _update_expense_categories(
        self, tenant_id: uuid.UUID, payload: dict[str, Any]
    ) -> None:
        category = payload.get("category") or payload.get("description", "")[:30]
        if not category:
            return
        current = await self._get_pattern(tenant_id, "common_expense_categories")
        counts: dict[str, int] = current.get("counts", {})
        counts[str(category)] = counts.get(str(category), 0) + 1
        top = sorted(counts, key=lambda k: counts[k], reverse=True)[:5]
        await self._upsert_pattern(
            tenant_id,
            "common_expense_categories",
            {"top": top, "counts": counts},
        )

    async def _update_top_action_types(self, tenant_id: uuid.UUID, action_type: str) -> None:
        current = await self._get_pattern(tenant_id, "top_action_types")
        counts: dict[str, int] = current.get("counts", {})
        counts[action_type] = counts.get(action_type, 0) + 1
        top = sorted(counts, key=lambda k: counts[k], reverse=True)[:5]
        await self._upsert_pattern(
            tenant_id,
            "top_action_types",
            {"top": top, "counts": counts},
        )

    # ── DB helpers ────────────────────────────────────────────────────────────

    async def _get_pattern(self, tenant_id: uuid.UUID, key: str) -> dict[str, Any]:
        try:
            res = await self._db.execute(
                select(AgentMemory).where(
                    AgentMemory.tenant_id == tenant_id,
                    AgentMemory.key == key,
                )
            )
            row = res.scalar_one_or_none()
            return row.value if row else {}  # type: ignore[return-value]
        except Exception:
            return {}

    async def _upsert_pattern(
        self, tenant_id: uuid.UUID, key: str, value: dict[str, Any]
    ) -> None:
        now = datetime.now(timezone.utc)
        res = await self._db.execute(
            select(AgentMemory).where(
                AgentMemory.tenant_id == tenant_id,
                AgentMemory.key == key,
            )
        )
        row = res.scalar_one_or_none()
        if row is None:
            row = AgentMemory(
                tenant_id=tenant_id,
                key=key,
                value=value,
                occurrence_count=1,
                confidence=1.0,
                last_seen_at=now,
            )
            self._db.add(row)
        else:
            row.value = value
            row.occurrence_count += 1
            row.last_seen_at = now
            # Confidence sube lentamente hacia 1.0 con más ocurrencias
            row.confidence = min(1.0, 0.5 + (row.occurrence_count / 20))
        await self._db.flush()

    async def _load_from_db(self, tenant_id: uuid.UUID) -> dict[str, Any]:
        try:
            res = await self._db.execute(
                select(AgentMemory).where(AgentMemory.tenant_id == tenant_id)
            )
            rows = res.scalars().all()
            data: dict[str, Any] = {row.key: row.value for row in rows}
            await self._set_cache(tenant_id, data)
            return data
        except Exception:
            logger.warning("agent_mem.db_load_failed", tenant_id=str(tenant_id))
            try:
                await self._db.rollback()
            except Exception:
                pass
            return {}

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
