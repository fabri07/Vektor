"""Telemetría de uso (panel SUPERADMIN, F5): un usuario, un día, una fila.

Se registra en `get_current_user`, en la MISMA sesión de la petición, así que la
fila confirma o se revierte junto con ella. El set en memoria sólo ahorra el
INSERT de las peticiones siguientes del mismo día; es una optimización y nunca la
fuente de verdad: la unicidad la da la clave primaria y el `ON CONFLICT DO NOTHING`.

Un cache que se marcara al insertar escondería la actividad si la petición se
revierte después (queda "visto" sin fila). Por eso se marca recién en
`after_commit`: si no hubo commit real, la próxima petición vuelve a intentar.

La telemetría nunca rompe un request: un fallo se loguea y se sigue.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date, timedelta

from sqlalchemy import Executable, event, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.business_time import now_ar, today_ar
from app.observability.logger import get_logger
from app.persistence.models.daily_activity import TenantDailyActivity
from app.persistence.models.user import User

logger = get_logger(__name__)

#: El soporte no es un "usuario del negocio": contarlo inflaría el uso real.
ROL_SIN_TELEMETRIA = "SUPERADMIN"

VENTANA_DIAS_ACTIVOS = 30

_vistos: set[tuple[uuid.UUID, date]] = set()


def _marcar_visto(clave: tuple[uuid.UUID, date]) -> None:
    # Una sola fecha viva: lo de días anteriores no vuelve a consultarse.
    for vieja in [k for k in _vistos if k[1] != clave[1]]:
        _vistos.discard(vieja)
    _vistos.add(clave)


def _insertar(session: AsyncSession, user: User, dia: date) -> Executable:
    valores = {
        "user_id": user.user_id,
        "activity_date": dia,
        "tenant_id": user.tenant_id,
        "created_at": now_ar(),
    }
    if session.get_bind().dialect.name == "postgresql":
        pg = pg_insert(TenantDailyActivity).values(**valores)
        return pg.on_conflict_do_nothing(index_elements=["user_id", "activity_date"])
    lite = sqlite_insert(TenantDailyActivity).values(**valores)
    return lite.on_conflict_do_nothing(index_elements=["user_id", "activity_date"])


async def registrar_actividad(session: AsyncSession, user: User) -> None:
    if user.role_code == ROL_SIN_TELEMETRIA:
        return
    dia = today_ar()
    clave = (user.user_id, dia)
    if clave in _vistos:
        return
    try:
        async with session.begin_nested():
            await session.execute(_insertar(session, user, dia))
    except Exception:  # noqa: BLE001 — la telemetría no tumba la petición
        logger.warning("activity_tracker.registro_fallido", exc_info=True)
        return
    event.listen(
        session.sync_session,
        "after_commit",
        lambda _s: _marcar_visto(clave),
        once=True,
    )


async def resumen_de_actividad(
    session: AsyncSession, tenant_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, tuple[date | None, int]]:
    """`{tenant_id: (último día con uso, días con uso en los últimos 30)}`.

    Una consulta para todos los tenants pedidos; los que no tienen filas
    devuelven `(None, 0)`.
    """
    resultado: dict[uuid.UUID, tuple[date | None, int]] = {t: (None, 0) for t in tenant_ids}
    if not tenant_ids:
        return resultado
    desde = today_ar() - timedelta(days=VENTANA_DIAS_ACTIVOS - 1)
    filas = await session.execute(
        select(
            TenantDailyActivity.tenant_id,
            func.max(TenantDailyActivity.activity_date),
            func.count(func.distinct(TenantDailyActivity.activity_date)).filter(
                TenantDailyActivity.activity_date >= desde
            ),
        )
        .where(TenantDailyActivity.tenant_id.in_(list(tenant_ids)))
        .group_by(TenantDailyActivity.tenant_id)
    )
    for tenant_id, ultimo, dias in filas.all():
        resultado[tenant_id] = (ultimo, int(dias or 0))
    return resultado
