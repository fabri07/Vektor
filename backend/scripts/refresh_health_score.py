"""Forzar un recálculo fresco del health score de un tenant (persiste snapshot).

Usage:
    export DATABASE_URL='postgresql://...neon.tech/neondb?sslmode=require'
    .venv/bin/python scripts/refresh_health_score.py fabriziosolafx@gmail.com

Por qué: el dashboard lee el último HealthScoreSnapshot persistido. Tras un
cambio de lógica (ej. fix de caja por cobertura), el snapshot viejo sigue
mostrándose hasta que corre un recálculo. Este script llama a
HealthScoreService.recalculate_for_tenant, que computa un BusinessState FRESCO
(usa _NullRedis → siempre cache-miss, no toca el Redis interno de Railway) y
persiste un snapshot nuevo con la lógica actual.

ESCRIBE: inserta un HealthScoreSnapshot + analytics_event + decision_audit_log
(idéntico a un recálculo disparado por Celery). NUNCA imprime la connection URL.
Correr desde backend/.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _db import async_engine_config  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

from app.application.services.health_score_service import HealthScoreService  # noqa: E402


async def main(email: str) -> None:
    url, connect_args = async_engine_config()
    engine = create_async_engine(url, connect_args=connect_args)
    async with AsyncSession(engine) as session:
        row = await session.execute(
            text("SELECT tenant_id FROM users WHERE lower(email) = lower(:e)"),
            {"e": email},
        )
        tid = row.scalar_one_or_none()
        if tid is None:
            print(f"ERROR: no existe usuario con email {email!r}.")
            sys.exit(1)
        tenant_id = uuid.UUID(str(tid))
        print(f"Recalculando health score para tenant {tenant_id} ...")

        svc = HealthScoreService(session)
        snap = await svc.recalculate_for_tenant(
            tenant_id, triggered_by="manual_cash_fix_refresh"
        )
        await session.commit()

        cash_source = (snap.score_inputs_json or {}).get("cash_source")
        print("\n  SNAPSHOT NUEVO:")
        print(f"    total_score={snap.total_score}  score_cash={snap.score_cash}")
        print(f"    cash_source={cash_source}")
        print(f"    primary_risk_code={snap.primary_risk_code}  level={snap.level}")
        print(f"    confidence={snap.confidence_level}  created_at={snap.created_at}")
        if cash_source == "flujo":
            print("\n  ✅ Caja por cobertura de flujo. El dashboard ya debería mostrarlo.")
        elif cash_source in (None, "desconocido"):
            print("\n  ⚠ cash_source quedó en", cash_source, "— revisar movimientos líquidos.")
    await engine.dispose()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: DATABASE_URL=... python scripts/refresh_health_score.py <email>")
        sys.exit(2)
    asyncio.run(main(sys.argv[1]))
